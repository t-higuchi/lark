from typing import TypeVar, Tuple, List, Callable, Generic, Type, Union, Optional, Any
from abc import ABC
from functools import wraps, update_wrapper

from .utils import combine_alternatives
from .tree import Tree
from .exceptions import VisitError, GrammarError
from .lexer import Token

###{standalone
from inspect import getmembers, getmro

_T = TypeVar('_T')
_R = TypeVar('_R')
_FUNC = Callable[..., _T]
_DECORATED = Union[_FUNC, type]

class Discard(Exception):
    """When raising the Discard exception in a transformer callback,
    that node is discarded and won't appear in the parent.
    """
    pass

# Transformers


class _Decoratable:
    "Provides support for decorating methods with @v_args"

    @classmethod
    def _apply_v_args(cls, visit_wrapper):
        mro = getmro(cls)
        assert mro[0] is cls
        libmembers = {name for _cls in mro[1:] for name, _ in getmembers(_cls)}
        for name, value in getmembers(cls):

            # Make sure the function isn't inherited (unless it's overwritten)
            if name.startswith('_') or (name in libmembers and name not in cls.__dict__):
                continue
            if not callable(value):
                continue

            # Skip if v_args already applied (at the function level)
            if isinstance(cls.__dict__[name], _VArgsWrapper):
                continue

            setattr(cls, name, _VArgsWrapper(cls.__dict__[name], visit_wrapper))
        return cls

    def __class_getitem__(cls, _):
        return cls


class Transformer(_Decoratable, ABC, Generic[_T]):
    """Transformers visit each node of the tree, and run the appropriate method on it according to the node's data.

    Methods are provided by the user via inheritance, and called according to ``tree.data``.
    The returned value from each method replaces the node in the tree structure.

    Transformers work bottom-up (or depth-first), starting with the leaves and ending at the root of the tree.
    Transformers can be used to implement map & reduce patterns. Because nodes are reduced from leaf to root,
    at any point the callbacks may assume the children have already been transformed (if applicable).

    ``Transformer`` can do anything ``Visitor`` can do, but because it reconstructs the tree,
    it is slightly less efficient.

    All these classes implement the transformer interface:

    - ``Transformer`` - Recursively transforms the tree. This is the one you probably want.
    - ``Transformer_InPlace`` - Non-recursive. Changes the tree in-place instead of returning new instances
    - ``Transformer_InPlaceRecursive`` - Recursive. Changes the tree in-place instead of returning new instances

    Parameters:
        visit_tokens (bool, optional): Should the transformer visit tokens in addition to rules.
                                       Setting this to ``False`` is slightly faster. Defaults to ``True``.
                                       (For processing ignored tokens, use the ``lexer_callbacks`` options)

    NOTE: A transformer without methods essentially performs a non-memoized partial deepcopy.
    """
    __visit_tokens__ = True   # For backwards compatibility

    def __init__(self,  visit_tokens: bool=True) -> None:
        self.__visit_tokens__ = visit_tokens

    def _call_userfunc(self, tree, new_children=None):
        # Assumes tree is already transformed
        children = new_children if new_children is not None else tree.children
        try:
            f = getattr(self, tree.data)
        except AttributeError:
            return self.__default__(tree.data, children, tree.meta)
        else:
            try:
                wrapper = getattr(f, 'visit_wrapper', None)
                if wrapper is not None:
                    return f.visit_wrapper(f, tree.data, children, tree.meta)
                else:
                    return f(children)
            except (GrammarError, Discard):
                raise
            except Exception as e:
                raise VisitError(tree.data, tree, e)

    def _call_userfunc_token(self, token):
        try:
            f = getattr(self, token.type)
        except AttributeError:
            return self.__default_token__(token)
        else:
            try:
                return f(token)
            except (GrammarError, Discard):
                raise
            except Exception as e:
                raise VisitError(token.type, token, e)

    def _transform_children(self, children):
        for c in children:
            try:
                if isinstance(c, Tree):
                    yield self._transform_tree(c)
                elif self.__visit_tokens__ and isinstance(c, Token):
                    yield self._call_userfunc_token(c)
                else:
                    yield c
            except Discard:
                pass

    def _transform_tree(self, tree):
        children = list(self._transform_children(tree.children))
        return self._call_userfunc(tree, children)

    def transform(self, tree: Tree) -> _T:
        "Transform the given tree, and return the final result"
        return self._transform_tree(tree)

    def __mul__(self, other: 'Transformer[_T]') -> 'TransformerChain[_T]':
        """Chain two transformers together, returning a new transformer.
        """
        return TransformerChain(self, other)

    def __default__(self, data, children, meta):
        """Default function that is called if there is no attribute matching ``data``

        Can be overridden. Defaults to creating a new copy of the tree node (i.e. ``return Tree(data, children, meta)``)
        """
        return Tree(data, children, meta)

    def __default_token__(self, token):
        """Default function that is called if there is no attribute matching ``token.type``

        Can be overridden. Defaults to returning the token as-is.
        """
        return token


def merge_transformers(base_transformer=None, **transformers_to_merge):
    """Merge a collection of transformers into the base_transformer, each into its own 'namespace'.

    When called, it will collect the methods from each transformer, and assign them to base_transformer,
    with their name prefixed with the given keyword, as ``prefix__methodname``.

    This function is especially useful for processing grammars that import other grammars,
    thereby creating some of their rules in a 'namespace'. (i.e with a consistent name prefix).
    In this case, the key for the transformer should match the name of the imported grammar.

    Parameters:
        base_transformer (Transformer, optional): The transformer that all other transformers will be added to.
        **transformers_to_merge: Keyword arguments, in the form of ``name_prefix = transformer``.

    Raises:
        AttributeError: In case of a name collision in the merged methods

    Example:
        ::

            class TBase(Transformer):
                def start(self, children):
                    return children[0] + 'bar'

            class TImportedGrammar(Transformer):
                def foo(self, children):
                    return "foo"

            composed_transformer = merge_transformers(TBase(), imported=TImportedGrammar())

            t = Tree('start', [ Tree('imported__foo', []) ])

            assert composed_transformer.transform(t) == 'foobar'

    """
    if base_transformer is None:
        base_transformer = Transformer()
    for prefix, transformer in transformers_to_merge.items():
        for method_name in dir(transformer):
            method = getattr(transformer, method_name)
            if not callable(method):
                continue
            if method_name.startswith("_") or method_name == "transform":
                continue
            prefixed_method = prefix + "__" + method_name
            if hasattr(base_transformer, prefixed_method):
                raise AttributeError("Cannot merge: method '%s' appears more than once" % prefixed_method)

            setattr(base_transformer, prefixed_method, method)

    return base_transformer


class InlineTransformer(Transformer):   # XXX Deprecated
    def _call_userfunc(self, tree, new_children=None):
        # Assumes tree is already transformed
        children = new_children if new_children is not None else tree.children
        try:
            f = getattr(self, tree.data)
        except AttributeError:
            return self.__default__(tree.data, children, tree.meta)
        else:
            return f(*children)

class TransformerChain(Generic[_T]):

    transformers: Tuple[Transformer[_T], ...]

    def __init__(self, *transformers: Transformer[_T]) -> None:
        self.transformers = transformers

    def transform(self, tree: Tree) -> _T:
        for t in self.transformers:
            tree = t.transform(tree)
        return tree

    def __mul__(self, other: Transformer[_T]) -> 'TransformerChain[_T]':
        return TransformerChain(*self.transformers + (other,))


class Transformer_InPlace(Transformer):
    """Same as Transformer, but non-recursive, and changes the tree in-place instead of returning new instances

    Useful for huge trees. Conservative in memory.
    """
    def _transform_tree(self, tree):           # Cancel recursion
        return self._call_userfunc(tree)

    def transform(self, tree):
        for subtree in tree.iter_subtrees():
            subtree.children = list(self._transform_children(subtree.children))

        return self._transform_tree(tree)


class Transformer_NonRecursive(Transformer):
    """Same as Transformer but non-recursive.

    Like Transformer, it doesn't change the original tree.

    Useful for huge trees.
    """

    def transform(self, tree):
        # Tree to postfix
        rev_postfix = []
        q = [tree]
        while q:
            t = q.pop()
            rev_postfix.append(t)
            if isinstance(t, Tree):
                q += t.children

        # Postfix to tree
        stack = []
        for x in reversed(rev_postfix):
            if isinstance(x, Tree):
                size = len(x.children)
                if size:
                    args = stack[-size:]
                    del stack[-size:]
                else:
                    args = []
                try:
                    stack.append(self._call_userfunc(x, args))
                except Discard:
                    pass
            elif self.__visit_tokens__ and isinstance(x, Token):
                try:
                    stack.append(self._call_userfunc_token(x))
                except Discard:
                    pass
            else:
                stack.append(x)

        t ,= stack  # We should have only one tree remaining
        return t


class Transformer_InPlaceRecursive(Transformer):
    "Same as Transformer, recursive, but changes the tree in-place instead of returning new instances"
    def _transform_tree(self, tree):
        tree.children = list(self._transform_children(tree.children))
        return self._call_userfunc(tree)


# Visitors

class VisitorBase:
    def _call_userfunc(self, tree):
        return getattr(self, tree.data, self.__default__)(tree)

    def __default__(self, tree):
        """Default function that is called if there is no attribute matching ``tree.data``

        Can be overridden. Defaults to doing nothing.
        """
        return tree

    def __class_getitem__(cls, _):
        return cls


class Visitor(VisitorBase, ABC, Generic[_T]):
    """Tree visitor, non-recursive (can handle huge trees).

    Visiting a node calls its methods (provided by the user via inheritance) according to ``tree.data``
    """

    def visit(self, tree: Tree) -> Tree:
        "Visits the tree, starting with the leaves and finally the root (bottom-up)"
        for subtree in tree.iter_subtrees():
            self._call_userfunc(subtree)
        return tree

    def visit_topdown(self, tree: Tree) -> Tree:
        "Visit the tree, starting at the root, and ending at the leaves (top-down)"
        for subtree in tree.iter_subtrees_topdown():
            self._call_userfunc(subtree)
        return tree


class Visitor_Recursive(VisitorBase):
    """Bottom-up visitor, recursive.

    Visiting a node calls its methods (provided by the user via inheritance) according to ``tree.data``

    Slightly faster than the non-recursive version.
    """

    def visit(self, tree: Tree) -> Tree:
        "Visits the tree, starting with the leaves and finally the root (bottom-up)"
        for child in tree.children:
            if isinstance(child, Tree):
                self.visit(child)

        self._call_userfunc(tree)
        return tree

    def visit_topdown(self,tree: Tree) -> Tree:
        "Visit the tree, starting at the root, and ending at the leaves (top-down)"
        self._call_userfunc(tree)

        for child in tree.children:
            if isinstance(child, Tree):
                self.visit_topdown(child)

        return tree


class Interpreter(_Decoratable, ABC, Generic[_T]):
    """Interpreter walks the tree starting at the root.

    Visits the tree, starting with the root and finally the leaves (top-down)

    For each tree node, it calls its methods (provided by user via inheritance) according to ``tree.data``.

    Unlike ``Transformer`` and ``Visitor``, the Interpreter doesn't automatically visit its sub-branches.
    The user has to explicitly call ``visit``, ``visit_children``, or use the ``@visit_children_decor``.
    This allows the user to implement branching and loops.
    """

    def visit(self, tree: Tree) -> _T:
        f = getattr(self, tree.data)
        wrapper = getattr(f, 'visit_wrapper', None)
        if wrapper is not None:
            return f.visit_wrapper(f, tree.data, tree.children, tree.meta)
        else:
            return f(tree)

    def visit_children(self, tree: Tree) -> List[_T]:
        return [self.visit(child) if isinstance(child, Tree) else child
                for child in tree.children]

    def __getattr__(self, name):
        return self.__default__

    def __default__(self, tree):
        return self.visit_children(tree)


_InterMethod = Callable[[Type[Interpreter], _T], _R]

def visit_children_decor(func: _InterMethod) -> _InterMethod:
    "See Interpreter"
    @wraps(func)
    def inner(cls, tree):
        values = cls.visit_children(tree)
        return func(cls, values)
    return inner

# Decorators

def _apply_v_args(obj, visit_wrapper):
    try:
        _apply = obj._apply_v_args
    except AttributeError:
        return _VArgsWrapper(obj, visit_wrapper)
    else:
        return _apply(visit_wrapper)


class _VArgsWrapper:
    """
    A wrapper around a Callable. It delegates `__call__` to the Callable.
    If the Callable has a `__get__`, that is also delegate and the resulting function is wrapped.
    Otherwise, we use the original function mirroring the behaviour without a __get__.
    We also have the visit_wrapper attribute to be used by Transformers.
    """
    def __init__(self, func: Callable, visit_wrapper: Callable[[Callable, str, list, Any], Any]):
        if isinstance(func, _VArgsWrapper):
            func = func.base_func
        self.base_func = func
        self.visit_wrapper = visit_wrapper
        update_wrapper(self, func)

    def __call__(self, *args, **kwargs):
        return self.base_func(*args, **kwargs)

    def __get__(self, instance, owner=None):
        try:
            g = self.base_func.__get__
        except AttributeError:
            return self
        else:
            return _VArgsWrapper(g(instance, owner), self.visit_wrapper)

    def __set_name__(self, owner, name):
        try:
            f = self.base_func.__set_name__
        except AttributeError:
            return
        else:
            f(owner, name)


def _vargs_inline(f, _data, children, _meta):
    return f(*children)
def _vargs_meta_inline(f, _data, children, meta):
    return f(meta, *children)
def _vargs_meta(f, _data, children, meta):
    return f(meta, children)
def _vargs_tree(f, data, children, meta):
    return f(Tree(data, children, meta))


def v_args(inline: bool = False, meta: bool = False, tree: bool = False, wrapper: Optional[Callable] = None) -> Callable[[_DECORATED], _DECORATED]:
    """A convenience decorator factory for modifying the behavior of user-supplied visitor methods.

    By default, callback methods of transformers/visitors accept one argument - a list of the node's children.

    ``v_args`` can modify this behavior. When used on a transformer/visitor class definition,
    it applies to all the callback methods inside it.

    ``v_args`` can be applied to a single method, or to an entire class. When applied to both,
    the options given to the method take precedence.

    Parameters:
        inline (bool, optional): Children are provided as ``*args`` instead of a list argument (not recommended for very long lists).
        meta (bool, optional): Provides two arguments: ``children`` and ``meta`` (instead of just the first)
        tree (bool, optional): Provides the entire tree as the argument, instead of the children.
        wrapper (function, optional): Provide a function to decorate all methods.

    Example:
        ::

            @v_args(inline=True)
            class SolveArith(Transformer):
                def add(self, left, right):
                    return left + right


            class ReverseNotation(Transformer_InPlace):
                @v_args(tree=True)
                def tree_node(self, tree):
                    tree.children = tree.children[::-1]
    """
    if tree and (meta or inline):
        raise ValueError("Visitor functions cannot combine 'tree' with 'meta' or 'inline'.")

    func = None
    if meta:
        if inline:
            func = _vargs_meta_inline
        else:
            func = _vargs_meta
    elif inline:
        func = _vargs_inline
    elif tree:
        func = _vargs_tree

    if wrapper is not None:
        if func is not None:
            raise ValueError("Cannot use 'wrapper' along with 'tree', 'meta' or 'inline'.")
        func = wrapper

    def _visitor_args_dec(obj):
        return _apply_v_args(obj, func)
    return _visitor_args_dec


###}


# --- Visitor Utilities ---

class CollapseAmbiguities(Transformer):
    """
    Transforms a tree that contains any number of _ambig nodes into a list of trees,
    each one containing an unambiguous tree.

    The length of the resulting list is the product of the length of all _ambig nodes.

    Warning: This may quickly explode for highly ambiguous trees.

    """
    def _ambig(self, options):
        return sum(options, [])

    def __default__(self, data, children_lists, meta):
        return [Tree(data, children, meta) for children in combine_alternatives(children_lists)]

    def __default_token__(self, t):
        return [t]
