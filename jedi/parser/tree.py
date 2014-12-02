"""
If you know what an abstract syntax tree (ast) is, you'll see that this module
is pretty much that. The classes represent syntax elements: ``Import``,
``Function``.

A very central class is ``Scope``. It is not used directly by the parser, but
inherited. It's used by ``Function``, ``Class``, ``Flow``, etc. A ``Scope`` may
have ``subscopes``, ``imports`` and ``statements``. The entire parser is based
on scopes, because they also stand for indentation.

One special thing:

``Array`` values are statements. But if you think about it, this makes sense.
``[1, 2+33]`` for example would be an Array with two ``Statement`` inside. This
is the easiest way to write a parser. The same behaviour applies to ``Param``,
which is being used in a function definition.

The easiest way to play with this module is to use :class:`parsing.Parser`.
:attr:`parsing.Parser.module` holds an instance of :class:`SubModule`:

>>> from jedi._compatibility import u
>>> from jedi.parser import Parser
>>> parser = Parser(u('import os'), 'example.py')
>>> submodule = parser.module
>>> submodule
<SubModule: example.py@1-1>

Any subclasses of :class:`Scope`, including :class:`SubModule` has
attribute :attr:`imports <Scope.imports>`.  This attribute has import
statements in this scope.  Check this out:

>>> submodule.imports
[<Import: import os @1,0>]

See also :attr:`Scope.subscopes` and :attr:`Scope.statements`.


# TODO New docstring

"""
import os
import re
from inspect import cleandoc
from collections import defaultdict
from itertools import chain

from jedi._compatibility import (next, Python3Method, encoding, is_py3,
                                 literal_eval, use_metaclass)
from jedi import debug
from jedi import cache


SCOPE_CONTENTS = 'asserts', 'subscopes', 'imports', 'statements', 'returns'


def is_node(node, *symbol_names):
    try:
        type = node.type
    except AttributeError:
        return False
    else:
        return type in symbol_names


def filter_after_position(names, position):
    """
    Removes all names after a certain position. If position is None, just
    returns the names list.
    """
    if position is None:
        return names

    names_new = []
    for n in names:
        if n.start_pos[0] is not None and n.start_pos < position:
            names_new.append(n)
    return names_new


class DocstringMixin(object):
    __slots__ = ()

    @property
    def raw_doc(self):
        """ Returns a cleaned version of the docstring token. """
        if isinstance(self, SubModule):
            stmt = self.children[0]
        else:
            stmt = self.children[-1]
            if is_node(stmt, 'suite'):  # Normally a suite
                stmt = stmt.children[2]  # -> NEWLINE INDENT stmt
        if is_node(stmt, 'simple_stmt'):
            stmt = stmt.children[0]

        try:
            first = stmt.children[0]
        except AttributeError:
            pass  # Probably a pass Keyword (Leaf).
        else:
            if isinstance(first, String):
                # TODO We have to check next leaves until there are no new
                # leaves anymore that might be part of the docstring. A
                # docstring can also look like this: ``'foo' 'bar'
                # Returns a literal cleaned version of the ``Token``.
                cleaned = cleandoc(literal_eval(first.value))
                # Since we want the docstr output to be always unicode, just
                # force it.
                if is_py3 or isinstance(cleaned, unicode):
                    return cleaned
                else:
                    return unicode(cleaned, 'UTF-8', 'replace')
        return ''


class Base(object):
    """
    This is just here to have an isinstance check, which is also used on
    evaluate classes. But since they have sometimes a special type of
    delegation, it is important for those classes to override this method.

    I know that there is a chance to do such things with __instancecheck__, but
    since Python 2.5 doesn't support it, I decided to do it this way.
    """
    __slots__ = ()

    def isinstance(self, *cls):
        return isinstance(self, cls)

    @Python3Method
    def get_parent_until(self, classes=(), reverse=False,
                         include_current=True):
        """
        Searches the parent "chain" until the object is an instance of
        classes. If classes is empty return the last parent in the chain
        (is without a parent).
        """
        if type(classes) not in (tuple, list):
            classes = (classes,)
        scope = self if include_current else self.parent
        while scope.parent is not None:
            # TODO why if classes?
            if classes and reverse != scope.isinstance(*classes):
                break
            scope = scope.parent
        return scope

    def get_parent_scope(self, include_flows=False):
        """
        Returns the underlying scope.
        """
        scope = self.parent
        while scope.parent is not None:
            if include_flows and isinstance(scope, Flow):
                return scope
            if scope.is_scope():
                break
            scope = scope.parent
        return scope

    def is_scope(self):
        # Default is not being a scope. Just inherit from Scope.
        return False


class Leaf(Base):
    __slots__ = ('value', 'parent', 'start_pos', 'prefix')

    def __init__(self, value, start_pos, prefix=''):
        self.value = value
        self.start_pos = start_pos
        self.prefix = prefix
        self.parent = None

    @property
    def end_pos(self):
        return self.start_pos[0], self.start_pos[1] + len(self.value)

    def get_code(self):
        return self.prefix + self.value

    def next_sibling(self):
        """
        The node immediately following the invocant in their parent's children
        list. If the invocant does not have a next sibling, it is None
        """
        # Can't use index(); we need to test by identity
        for i, child in enumerate(self.parent.children):
            if child is self:
                try:
                    return self.parent.children[i + 1]
                except IndexError:
                    return None

    def prev_sibling(self):
        """
        The node immediately preceding the invocant in their parent's children
        list. If the invocant does not have a previous sibling, it is None.
        """
        # Can't use index(); we need to test by identity
        for i, child in enumerate(self.parent.children):
            if child is self:
                if i == 0:
                    return None
                return self.parent.children[i - 1]

    def __repr__(self):
        return "<%s: %s>" % (type(self).__name__, repr(self.value))


class Whitespace(Leaf):
    type = 'whitespace'
    """Contains NEWLINE and ENDMARKER tokens."""


class Name(Leaf):
    """
    A string. Sometimes it is important to know if the string belongs to a name
    or not.
    """
    type = 'name'
    def __str__(self):
        return self.value

    def __unicode__(self):
        return self.value

    def __repr__(self):
        return "<%s: %s@%s,%s>" % (type(self).__name__, self.value,
                                   self.start_pos[0], self.start_pos[1])

    def get_definition(self):
        scope = self.parent
        while scope.parent is not None:
            if scope.isinstance(Node):
                if scope.type == 'testlist_comp':
                    try:
                        if isinstance(scope.children[1], CompFor):
                            return scope.children[1]
                    except IndexError:
                        pass
            else:
                break
            scope = scope.parent
        return scope

    def is_definition(self):
        stmt = self.get_definition()
        if isinstance(stmt, (Function, Class, Module)):
            return self == stmt.name
        elif isinstance(stmt, ForStmt):
            return self.start_pos < stmt.children[2].start_pos
        elif isinstance(stmt, Param):
            return self == stmt.get_name()
        elif isinstance(stmt, TryStmt):
            return self.prev_sibling() == 'as'
        else:
            return isinstance(stmt, (ExprStmt, Import, CompFor, WithStmt)) \
                and self in stmt.get_defined_names()

    def assignment_indexes(self):
        """
        Returns an array of ints of the indexes that are used in tuple
        assignments.

        For example if the name is ``y`` in the following code::

            x, (y, z) = 2, ''

        would result in ``[1, 0]``.
        """
        indexes = []
        node = self.parent
        compare = self
        while node is not None:
            if is_node(node, 'testlist_comp', 'testlist_star_expr', 'exprlist'):
                for i, child in enumerate(node.children):
                    if child == compare:
                        indexes.insert(0, int(i / 2))
                        break
                else:
                    raise LookupError("Couldn't find the assignment.")
            elif isinstance(node, (ExprStmt, CompFor)):
                break

            compare = node
            node = node.parent
        return indexes


class Literal(Leaf):
    def eval(self):
        return literal_eval(self.value)

    def __repr__(self):
        # TODO remove?
        """
        if is_py3:
            s = self.literal
        else:
            s = self.literal.encode('ascii', 'replace')
        """
        return "<%s: %s>" % (type(self).__name__, self.value)


class Number(Literal):
    type = 'number'


class String(Literal):
    type = 'string'


class Operator(Leaf):
    type = 'operator'
    def __str__(self):
        return self.value

    def __eq__(self, other):
        """
        Make comparisons with strings easy.
        Improves the readability of the parser.
        """
        if isinstance(other, Operator):
            return self is other
        else:
            return self.value == other

    def __ne__(self, other):
        """Python 2 compatibility."""
        return self.value != other

    def __hash__(self):
        return hash(self.value)


class Keyword(Leaf):
    type = 'keyword'
    def __eq__(self, other):
        """
        Make comparisons with strings easy.
        Improves the readability of the parser.
        """
        if isinstance(other, Keyword):
            return self is other
        return self.value == other

    def __ne__(self, other):
        """Python 2 compatibility."""
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.value)


class Simple(Base):
    """
    The super class for Scope, Import, Name and Statement. Every object in
    the parser tree inherits from this class.
    """
    __slots__ = ('children', 'parent')

    def __init__(self, children):
        """
        Initialize :class:`Simple`.

        :param children: The module in which this Python object locates.
        """
        for c in children:
            c.parent = self
        self.children = children
        self.parent = None

    def move(self, line_offset, column_offset):
        """
        Move the Node's start_pos.
        """
        for c in self.children:
            if isinstance(c, Leaf):
                c.start_pos = (c.start_pos[0] + line_offset,
                               c.start_pos[1] + column_offset)
            else:
                c.move(line_offset, column_offset)

    @property
    def start_pos(self):
        return self.children[0].start_pos

    @property
    def _sub_module(self):
        return self.get_parent_until()

    @property
    def end_pos(self):
        return self.children[-1].end_pos

    def get_code(self):
        return "".join(c.get_code() for c in self.children)

    def name_for_position(self, position):
        for c in self.children:
            if isinstance(c, Leaf):
                if c.start_pos <= position <= c.end_pos:
                    return c
            else:
                result = c.name_for_position(position)
                if result is not None:
                    return result
        return None

    def first_leaf(self):
        try:
            return self.children[0].first_leaf()
        except AttributeError:
            return self.children[0]

    def __repr__(self):
        code = self.get_code().replace('\n', ' ')
        if not is_py3:
            code = code.encode(encoding, 'replace')
        return "<%s: %s@%s,%s>" % \
            (type(self).__name__, code, self.start_pos[0], self.start_pos[1])


class Node(Simple):
    """Concrete implementation for interior nodes."""

    def __init__(self, type, children):
        """
        Initializer.

        Takes a type constant (a symbol number >= 256), a sequence of
        child nodes, and an optional context keyword argument.

        As a side effect, the parent pointers of the children are updated.
        """
        super(Node, self).__init__(children)
        self.type = type

    def __repr__(self):
        return "%s(%s, %r)" % (self.__class__.__name__, self.type, self.children)


class IsScopeMeta(type):
    def __instancecheck__(self, other):
        return other.is_scope()


class IsScope(use_metaclass(IsScopeMeta)):
    pass


def _return_empty_list():
    """
    Necessary for pickling. It needs to be reachable for pickle, cannot
    be a lambda or a closure.
    """
    return []


class Scope(Simple, DocstringMixin):
    """
    Super class for the parser tree, which represents the state of a python
    text file.
    A Scope manages and owns its subscopes, which are classes and functions, as
    well as variables and imports. It is used to access the structure of python
    files.

    :param start_pos: The position (line and column) of the scope.
    :type start_pos: tuple(int, int)
    """
    __slots__ = ('_doc_token', 'names_dict')

    def __init__(self, children):
        super(Scope, self).__init__(children)
        self._doc_token = None

    @property
    def returns(self):
        # Needed here for fast_parser, because the fast_parser splits and
        # returns will be in "normal" modules.
        return self._search_in_scope(ReturnStmt)

    @property
    def asserts(self):
        # Needed here for fast_parser, because the fast_parser splits and
        # returns will be in "normal" modules.
        return self._search_in_scope(AssertStmt)

    @property
    def subscopes(self):
        return self._search_in_scope(Scope)

    @property
    def flows(self):
        return self._search_in_scope(Flow)

    @property
    def imports(self):
        return self._search_in_scope(Import)

    def _search_in_scope(self, typ):
        def scan(children):
            elements = []
            for element in children:
                if isinstance(element, typ):
                    elements.append(element)
                if is_node(element, 'suite', 'simple_stmt', 'decorated') \
                        or isinstance(element, Flow):
                    elements += scan(element.children)
            return elements

        return scan(self.children)

    @property
    def statements(self):
        return self._search_in_scope((ExprStmt, KeywordStatement))

    def is_scope(self):
        return True

    def get_imports(self):
        """ Gets also the imports within flow statements """
        raise NotImplementedError
        return []
        i = [] + self.imports
        for s in self.statements:
            if isinstance(s, Scope):
                i += s.get_imports()
        return i

    @Python3Method
    def get_defined_names(self):
        """
        Get all defined names in this scope. Useful for autocompletion.

        >>> from jedi._compatibility import u
        >>> from jedi.parser import Parser
        >>> parser = Parser(u('''
        ... a = x
        ... b = y
        ... b.c = z
        ... '''))
        >>> parser.module.get_defined_names()
        [<Name: a@2,0>, <Name: b@3,0>, <Name: b.c@4,0>]
        """
        def scan(children):
            names = []
            for c in children:
                if is_node(c, 'simple_stmt'):
                    names += chain.from_iterable(
                        [s.get_defined_names() for s in c.children
                         if isinstance(s, (ExprStmt, Import))])
                elif isinstance(c, (Function, Class)):
                    names.append(c.name)
                elif isinstance(c, Flow) or is_node(c, 'suite', 'decorated'):
                    names += scan(c.children)
            return names

        children = self.children
        return scan(children)

    @Python3Method
    def get_statement_for_position(self, pos, include_imports=False):
        checks = self.statements + self.asserts
        if include_imports:
            checks += self.imports
        if self.isinstance(Function):
            checks += self.get_decorators()
            checks += [r for r in self.returns if r is not None]

        for s in checks:
            if isinstance(s, Flow):
                p = s.get_statement_for_position(pos, include_imports)
                while s.next and not p:
                    s = s.next
                    p = s.get_statement_for_position(pos, include_imports)
                if p:
                    return p
            elif s.start_pos <= pos <= s.end_pos:
                return s

        for s in self.subscopes:
            if s.start_pos <= pos <= s.end_pos:
                p = s.get_statement_for_position(pos, include_imports)
                if p:
                    return p

    def __repr__(self):
        try:
            name = self.path
        except AttributeError:
            try:
                name = self.name
            except AttributeError:
                name = self.command

        return "<%s: %s@%s-%s>" % (type(self).__name__, name,
                                   self.start_pos[0], self.end_pos[0])

    def walk(self):
        yield self
        for s in self.subscopes:
            for scope in s.walk():
                yield scope

        for r in self.statements:
            while isinstance(r, Flow):
                for scope in r.walk():
                    yield scope
                r = r.next


class Module(Base):
    """
    For isinstance checks. fast_parser.Module also inherits from this.
    """
    def is_scope(self):
        return True


class SubModule(Scope, Module):
    """
    The top scope, which is always a module.
    Depending on the underlying parser this may be a full module or just a part
    of a module.
    """
    __slots__ = ('path', 'global_names', 'used_names',
                 'line_offset', 'use_as_parent', 'failed_statement_stacks')

    def __init__(self, children):
        """
        Initialize :class:`SubModule`.

        :type path: str
        :arg  path: File path to this module.

        .. todo:: Document `top_module`.
        """
        super(SubModule, self).__init__(children)
        self.path = None  # Set later.
        # this may be changed depending on fast_parser
        self.line_offset = 0

        if 0:
            self.use_as_parent = top_module or self

    def set_global_names(self, names):
        """
        Global means in these context a function (subscope) which has a global
        statement.
        This is only relevant for the top scope.

        :param names: names of the global.
        :type names: list of Name
        """
        self.global_names = names

    def add_global(self, name):
        # set no parent here, because globals are not defined in this scope.
        self.global_vars.append(name)

    def get_defined_names(self):
        n = super(SubModule, self).get_defined_names()
        # TODO uncomment
        #n += self.global_names
        return n

    @property
    @cache.underscore_memoization
    def name(self):
        """ This is used for the goto functions. """
        if self.path is None:
            string = ''  # no path -> empty name
        else:
            sep = (re.escape(os.path.sep),) * 2
            r = re.search(r'([^%s]*?)(%s__init__)?(\.py|\.so)?$' % sep, self.path)
            # Remove PEP 3149 names
            string = re.sub('\.[a-z]+-\d{2}[mud]{0,3}$', '', r.group(1))
        # Positions are not real, but a module starts at (1, 0)
        p = (1, 0)
        name = Name(string, p)
        name.parent = self
        return name

    @property
    def has_explicit_absolute_import(self):
        """
        Checks if imports in this module are explicitly absolute, i.e. there
        is a ``__future__`` import.
        """
        for imp in self.imports:
            

            # TODO implement!
            continue
            if not imp.from_names or not imp.namespace_names:
                continue

            namespace, feature = imp.from_names[0], imp.namespace_names[0]
            if unicode(namespace) == "__future__" and unicode(feature) == "absolute_import":
                return True

        return False


class Decorator(Simple):
    type = 'decorator'
    pass


class ClassOrFunc(Scope):
    __slots__ = ()

    @property
    def name(self):
        return self.children[1]

    def get_decorators(self):
        decorated = self.parent
        if is_node(decorated, 'decorated'):
            if is_node(decorated.children[0], 'decorators'):
                return decorated.children[0].children
            else:
                return decorated.children[:1]
        else:
            return []


class Class(ClassOrFunc):
    """
    Used to store the parsed contents of a python class.

    :param name: The Class name.
    :type name: str
    :param supers: The super classes of a Class.
    :type supers: list
    :param start_pos: The start position (line, column) of the class.
    :type start_pos: tuple(int, int)
    """
    type = 'classdef'

    def __init__(self, children):
        super(Class, self).__init__(children)

    def get_super_arglist(self):
        if len(self.children) == 4:  # Has no parentheses
            return None
        else:
            if self.children[3] == ')':  # Empty parentheses
                return None
            else:
                return self.children[3]

    @property
    def doc(self):
        """
        Return a document string including call signature of __init__.
        """
        docstr = ""
        if self._doc_token is not None:
            docstr = self.raw_doc
        for sub in self.subscopes:
            if unicode(sub.name) == '__init__':
                return '%s\n\n%s' % (
                    sub.get_call_signature(funcname=self.name), docstr)
        return docstr

    def scope_names_generator(self, position=None):
        yield self, filter_after_position(self.get_defined_names(), position)


def _create_params(function, lst):
    if not lst:
        return []
    if is_node(lst[0], 'typedargslist', 'varargslist'):
        params = []
        iterator = iter(lst[0].children)
        for n in iterator:
            stars = 0
            if n in ('*', '**'):
                stars = len(n.value)
                n = next(iterator)

            op = next(iterator, None)
            if op == '=':
                default = next(iterator)
                next(iterator, None)
            else:
                default = None
            params.append(Param(n, function, default, stars))
        return params
    else:
        return [Param(lst[0], function)]


class Function(ClassOrFunc):
    """
    Used to store the parsed contents of a python function.

    :param name: The Function name.
    :type name: str
    :param params: The parameters (Statement) of a Function.
    :type params: list
    :param start_pos: The start position (line, column) the Function.
    :type start_pos: tuple(int, int)
    """
    __slots__ = ('listeners', 'params')
    type = 'funcdef'

    def __init__(self, children):
        super(Function, self).__init__(children)
        self.listeners = set()  # not used here, but in evaluation.
        lst = self.children[2].children[1:-1]  # After `def foo`
        self.params = _create_params(self, lst)

    @property
    def name(self):
        return self.children[1]  # First token after `def`

    @property
    def yields(self):
        # TODO This is incorrect, yields are also possible in a statement.
        return self._search_in_scope(YieldExpr)

    def is_generator(self):
        return bool(self.yields)

    def annotation(self):
        try:
            return self.children[6]  # 6th element: def foo(...) -> bar
        except IndexError:
            return None

    def get_defined_names(self):
        n = super(Function, self).get_defined_names()
        for p in self.params:
            try:
                n.append(p.get_name())
            except IndexError:
                debug.warning("multiple names in param %s", n)
        return n

    def scope_names_generator(self, position=None):
        yield self, filter_after_position(self.get_defined_names(), position)

    def get_call_signature(self, width=72, funcname=None):
        """
        Generate call signature of this function.

        :param width: Fold lines if a line is longer than this value.
        :type width: int
        :arg funcname: Override function name when given.
        :type funcname: str

        :rtype: str
        """
        l = unicode(funcname or self.name) + '('
        lines = []
        for (i, p) in enumerate(self.params):
            code = p.get_code(False)
            if i != len(self.params) - 1:
                code += ', '
            if len(l + code) > width:
                lines.append(l[:-1] if l[-1] == ' ' else l)
                l = code
            else:
                l += code
        if l:
            lines.append(l)
        lines[-1] += ')'
        return '\n'.join(lines)

    @property
    def doc(self):
        """ Return a document string including call signature. """
        docstr = ""
        if self._doc_token is not None:
            docstr = self.raw_doc
        return '%s\n\n%s' % (self.get_call_signature(), docstr)


class Lambda(Function):
    """
    Lambdas are basically trimmed functions, so give it the same interface.
    """
    type = 'lambda'
    def __init__(self, children):
        super(Function, self).__init__(children)
        self.listeners = set()  # not used here, but in evaluation.
        lst = self.children[1:-2]  # After `def foo`
        self.params = _create_params(self, lst)

    def is_generator(self):
        return False

    def yields(self):
        return []

    def __repr__(self):
        return "<%s@%s>" % (self.__class__.__name__, self.start_pos)


class Flow(Simple):
    pass


class IfStmt(Flow):
    type = 'if_stmt'
    def check_nodes(self):
        """
        Returns all the `test` nodes that are defined as x, here:

            if x:
                pass
            elif x:
                pass
        """
        for i, c in enumerate(self.children):
            if c in ('elif', 'if'):
                yield self.children[i + 1]

    def node_in_which_check_node(self, node):
        for check_node in reversed(list(self.check_nodes())):
            if check_node.start_pos < node.start_pos:
                return check_node

    def node_after_else(self, node):
        """
        Checks if a node is defined after `else`.
        """
        for c in self.children:
            if c == 'else':
                if node.start_pos > c.start_pos:
                    return True
        else:
            return False


class WhileStmt(Flow):
    type = 'while_stmt'


class ForStmt(Flow):
    type = 'for_stmt'


class TryStmt(Flow):
    type = 'try_stmt'


class WithStmt(Flow):
    type = 'with_stmt'
    def get_defined_names(self):
        names = []
        for with_item in self.children[1:-2:2]:
            # Check with items for 'as' names.
            if is_node(with_item, 'with_item'):
                names += _defined_names(with_item.children[2])
        return names

    def node_from_name(self, name):
        node = name
        while True:
            node = node.parent
            if is_node(node, 'with_item'):
                return node.children[0]


class Import(Simple):
    def get_all_import_names(self):
        # TODO remove. do we even need this?
        raise NotImplementedError

    def path_for_name(self, name):
        try:
            # The name may be an alias. If it is, just map it back to the name.
            name = self.aliases()[name]
        except KeyError:
            pass

        for path in self._paths():
            if name in path:
                return path[:path.index(name) + 1]
        raise ValueError('Name should be defined in the import itself')

    def is_nested(self):
        """
        This checks for the special case of nested imports, without aliases and
        from statement::

            import foo.bar
        """
        return False
        # TODO use this check differently?
        return not self.alias and not self.from_names \
            and len(self.namespace_names) > 1

    def is_star_import(self):
        return self.children[-1] == '*'


class ImportFrom(Import):
    type = 'import_from'
    def get_defined_names(self):
        return [alias or name for name, alias in self._as_name_tuples()]

    def aliases(self):
        """Mapping from alias to its corresponding name."""
        return dict((alias, name) for name, alias in self._as_name_tuples()
                    if alias is not None)

    @property
    def level(self):
        """The level parameter of ``__import__``."""
        level = 0
        for n in self.children[1:]:
            if n in ('.', '...'):
                level += len(n.value)
            else:
                break
        return level

    def _as_name_tuples(self):
        last = self.children[-1]
        if last == ')':
            last = self.children[-2]
        elif last == '*':
            return  # No names defined directly.

        if is_node(last, 'import_as_names'):
            as_names = last.children[::2]
        else:
            as_names = [last]
        for as_name in as_names:
            if isinstance(as_name, Name):
                yield as_name, None
            else:
                yield as_name.children[::2]  # yields x, y -> ``x as y``

    def star_import_name(self):
        """
        The last name defined in a star import.
        """
        return self._paths()[-1][-1]

    def _paths(self):
        for n in self.children[1:]:
            if n not in ('.', '...'):
                break
        if is_node(n, 'dotted_name'):  # from x.y import
            dotted = n.children[::2]
        elif n == 'import':  # from . import
            dotted = []
        else:  # from x import
            dotted = [n]

        if self.children[-1] == '*':
            return [dotted]
        return [dotted + [name] for name, alias in self._as_name_tuples()]


class ImportName(Import):
    """For ``import_name`` nodes. Covers normal imports without ``from``."""
    type = 'import_name'

    def get_defined_names(self):
        return [alias or path[0] for path, alias in self._dotted_as_names()]

    @property
    def level(self):
        """The level parameter of ``__import__``."""
        return 0  # Obviously 0 for imports without from.

    def _paths(self):
        return [path for path, alias in self._dotted_as_names()]

    def _dotted_as_names(self):
        """Generator of (list(path), alias) where alias may be None."""
        dotted_as_names = self.children[1]
        if is_node(dotted_as_names, 'dotted_as_names'):
            as_names = dotted_as_names.children[::2]
        else:
            as_names = [dotted_as_names]

        for as_name in as_names:
            if is_node(as_name, 'dotted_as_name'):
                alias = as_name.children[2]
                as_name = as_name.children[0]
            else:
                alias = None
            if isinstance(as_name, Name):
                yield [as_name], alias
            else:
                # dotted_names
                yield as_name.children[::2], alias

    def aliases(self):
        return dict((alias, path[-1]) for path, alias in self._dotted_as_names()
                    if alias is not None)


class KeywordStatement(Simple):
    """
    For the following statements: `assert`, `del`, `global`, `nonlocal`,
    `raise`, `return`, `yield`, `pass`, `continue`, `break`, `return`, `yield`.
    """
    @property
    def keyword(self):
        return self.children[0].value


class AssertStmt(KeywordStatement):
    type = 'assert_stmt'
    def assertion(self):
        return self.children[1]


class GlobalStmt(KeywordStatement):
    type = 'global_stmt'
    def get_defined_names(self):
        return self.children[1::2]


class ReturnStmt(KeywordStatement):
    type = 'return_stmt'


class YieldExpr(Simple):
    type = 'yield_expr'


def _defined_names(current):
    """
    A helper function to find the defined names in statements, for loops and
    list comprehensions.
    """
    names = []
    if is_node(current, 'testlist_star_expr', 'testlist_comp', 'exprlist'):
        for child in current.children[::2]:
            names += _defined_names(child)
    elif is_node(current, 'atom'):
        names += _defined_names(current.children[1])
    elif is_node(current, 'power'):
        if current.children[-2] != '**':  # Just if there's no operation
            trailer = current.children[-1]
            if trailer.children[0] == '.':
                names.append(trailer.children[1])
    else:
        names.append(current)
    return names


class Statement(Simple, DocstringMixin):
    """
    This is the class for all the possible statements. Which means, this class
    stores pretty much all the Python code, except functions, classes, imports,
    and flow functions like if, for, etc.

    :type  token_list: list
    :param token_list:
        List of tokens or names.  Each element is either an instance
        of :class:`Name` or a tuple of token type value (e.g.,
        :data:`tokenize.NUMBER`), token string (e.g., ``'='``), and
        start position (e.g., ``(1, 0)``).
    :type   start_pos: 2-tuple of int
    :param  start_pos: Position (line, column) of the Statement.
    """
    __slots__ = ()

    def get_defined_names(self):
        return list(chain.from_iterable(_defined_names(self.children[i])
                                        for i in range(0, len(self.children) - 2, 2)
                                        if '=' in self.children[i + 1].value))

    def get_rhs(self):
        """Returns the right-hand-side of the equals."""
        return self.children[-1]

    def get_names_dict(self):
        """The future of name resolution. Returns a dict(str -> Call)."""
        dct = defaultdict(lambda: [])

        def search_calls(calls):
            for call in calls:
                if isinstance(call, Array) and call.type != Array.DICT:
                    for stmt in call:
                        search_calls(stmt.expression_list())
                elif isinstance(call, Call):
                    c = call
                    # Check if there's an execution in it, if so this is
                    # not a set_var.
                    while True:
                        if c.next is None or isinstance(c.next, Array):
                            break
                        c = c.next
                    dct[unicode(c.name)].append(call)

        for calls, operation in self.assignment_details:
            search_calls(calls)

        if not self.assignment_details and self._names_are_set_vars:
            # In the case of Param, it's also a defining name without ``=``
            search_calls(self.expression_list())

        for as_name in self.as_names:
            dct[unicode(as_name)].append(Call(self._sub_module, as_name,
                                         as_name.start_pos, as_name.end_pos, self))
        return dct

    def first_operation(self):
        """
        Returns `+=`, `=`, etc or None if there is no operation.
        """
        try:
            return self.children[1]
        except IndexError:
            return None


class ExprStmt(Statement):
    """
    This class exists temporarily, to be able to distinguish real statements
    (``small_stmt`` in Python grammar) from the so called ``test`` parts, that
    may be used to defined part of an array, but are never a whole statement.

    The reason for this class is purely historical. It was easier to just use
    Statement nested, than to create a new class for Test (plus Jedi's fault
    tolerant parser just makes things very complicated).
    """
    type = 'expr_stmt'


class Param(Base):
    """
    The class which shows definitions of params of classes and functions.
    But this is not to define function calls.

    A helper class for functions. Read only.
    """
    __slots__ = ('tfpdef', 'default', 'stars', 'parent')

    def __init__(self, tfpdef, parent, default=None, stars=0):
        self.tfpdef = tfpdef  # tfpdef: see grammar.txt
        self.default = default
        self.stars = stars
        self.parent = parent
        # Here we reset the parent of our name. IMHO this is ok.
        self.get_name().parent = self

    def annotation(self):
        # Generate from tfpdef.
        raise NotImplementedError

    @property
    def children(self):
        return []

    @property
    def start_pos(self):
        return self.tfpdef.start_pos

    def get_name(self):
        # TODO remove!
        return self.name

    @property
    def name(self):
        if is_node(self.tfpdef, 'tfpdef'):
            return self.tfpdef.children[0]
        else:
            return self.tfpdef

    @property
    def position_nr(self):
        return self.parent.params.index(self)

    @property
    def parent_function(self):
        return self.get_parent_until(IsScope)

    def get_code(self):
        df = '' if self.default is None else '=' + self.default.get_code()
        return self.tfpdef.get_code() + df

    def add_annotation(self, annotation_stmt):
        annotation_stmt.parent = self.use_as_parent
        self.annotation_stmt = annotation_stmt

    def __repr__(self):
        default = '' if self.default is None else '=%s' % self.default
        return '<%s: %s>' % (type(self).__name__, str(self.tfpdef) + default)


class Array(object):
    # TODO remove this. Just here because we need these names.
    NOARRAY = None  # just brackets, like `1 * (3 + 2)`
    TUPLE = 'tuple'
    LIST = 'list'
    DICT = 'dict'
    SET = 'set'


class CompFor(Simple):
    type = 'comp_for'

    def is_scope(self):
        return True

    @property
    def names_dict(self):
        dct = {}
        for name in self.get_defined_names():
            arr = dct.setdefault(name.value, [])
            arr.append(name)
        return dct

    def get_rhs(self):
        return self.children[3]

    def get_defined_names(self):
        return _defined_names(self.children[1])

    def scope_names_generator(self, position):
        yield self, []

    @property
    def asserts(self):
        """Since it's a scope, it can be asked for asserts."""
        return []
