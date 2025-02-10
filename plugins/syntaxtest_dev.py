from collections import namedtuple
import logging
import re
from os import path
from pathlib import Path

import sublime
import sublime_plugin

from sublime_lib.flags import RegionOption

from .lib import get_setting, path_is_relative_to

__all__ = (
    'SyntaxTestHighlighterListener',
    'PackagedevAlignSyntaxTestCommand',
    'PackagedevSuggestSyntaxTestCommand',
    'AssignSyntaxTestSyntaxListener',
    'PackagedevGenerateSyntaxTestsForLineCommand',
)

logger = logging.getLogger(__name__)

AssertionLineDetails = namedtuple(
    'AssertionLineDetails', ['comment_marker_match', 'assertion_colrange', 'line_region']
)
SyntaxTestHeader = namedtuple(
    'SyntaxTestHeader', ['comment_start', 'comment_end', 'syntax_file', 'test_mode']
)


syntax_test_header_regex = re.compile(
    r'''
    ^(?P<comment_start>\s*.+?)
    \s+SYNTAX[ ]TEST\s+
    (?P<test_mode>(?:
        (?:
            partial-symbols
        |   (?:reindent(?:-un(?:indented|changed))?)
        )\s+)*
    )
    "(?P<syntax_file>[^"]+)"\s*
    (?P<comment_end>\S+)?$''',
    re.X
)


def get_syntax_test_tokens(view):
    """Parse the first line of the given view into a SyntaxTestHeader.

    Returns `None` if the file doesn't contain syntax tests.
    """

    line = view.line(0)
    match = None
    if line.size() < 1000:  # no point checking longer lines as they are unlikely to match
        first_line = view.substr(line)
        match = syntax_test_header_regex.match(first_line)

    if not match:
        return None
    else:
        return SyntaxTestHeader(**match.groupdict())


class SyntaxTestHighlighterListener(sublime_plugin.ViewEventListener):

    # TODO multiple views into the same file

    @classmethod
    def is_applicable(cls, settings):
        """Disable the listener completely when tabs are used."""
        return settings.get('translate_tabs_to_spaces', False)

    def __init__(self, view):
        super().__init__(view)
        self.header = None
        self.on_modified_async()
        self.on_selection_modified_async()

    def __del__(self):
        # Settings were (most likely) changed to use tabs
        # or plugin was unloaded.
        # Complain about the former, if we have a test file.
        if self.header and not self.is_applicable(self.view.settings()):
            sublime.status_message((
                "Syntax tests do not work properly with tabs as indentation."
                " You MUST use spaces!"
            ))

    def on_modified_async(self):
        """If the view has a filename, and that file name starts with
        the syntax test file prefix, or the view has no filename yet,
        read the first line to determine the syntax test header.
        """

        name = self.view.file_name()
        if name and not path.basename(name).startswith('syntax_test'):
            self.header = None
            return
        self.header = get_syntax_test_tokens(self.view)
        # if there is no comment start token, clear the header
        if self.header and not self.header.comment_start:
            self.header = None

    def get_details_of_test_assertion_line(self, pos):
        """Parse details of a test assertion at a given position in the view.

        Always returns a AssertionLineDetails instance, whose fields may be `None`.
        """

        tokens = self.header
        if not tokens:
            return AssertionLineDetails(None, None, None)
        line_region = self.view.line(pos)
        line_text = self.view.substr(line_region)
        test_start_token = re.match(fr'^\s*({re.escape(tokens.comment_start)})', line_text)
        assertion_colrange = None
        if test_start_token:
            assertion = re.match(r'\s*(?:(<-)|(\^+))', line_text[test_start_token.end():])
            if assertion:
                if assertion.group(1):
                    assertion_colrange = (test_start_token.start(1),
                                          test_start_token.start(1))
                elif assertion.group(2):
                    assertion_colrange = (test_start_token.end() + assertion.start(2),
                                          test_start_token.end() + assertion.end(2))

        return AssertionLineDetails(test_start_token, assertion_colrange, line_region)

    def is_syntax_test_line(self, pos, must_contain_assertion):
        """Determine whether the line at the given character position is a syntax test line.

        It can optionally treat lines with comment markers but no assertion as a syntax test,
        useful for while the line is being written.
        """

        details = self.get_details_of_test_assertion_line(pos)
        if details.comment_marker_match:
            return not must_contain_assertion or details.assertion_colrange is not None
        return False

    def get_details_of_line_being_tested(self):
        """Starting from the cursor position, work upwards to find all syntax
         test lines that occur before the line that is being tested.
        Return a tuple containing a list of assertion line details,
        along with the region of the line being tested.
        """

        if not self.header:
            return (None, None)

        lines = []
        pos = self.view.sel()[0].begin()
        first_line = True
        while pos >= 0:
            details = self.get_details_of_test_assertion_line(pos)
            pos = details.line_region.begin() - 1
            if details.assertion_colrange:
                lines.append(details)
            elif not first_line or not details.comment_marker_match:
                break
            elif details.comment_marker_match:
                lines.append(details)
            first_line = False

        return (lines, details.line_region)

    def on_selection_modified_async(self):
        """Update highlighting of what the current line's test assertions point at."""

        if not self.header or len(self.view.sel()) == 0:
            return

        lines, line = self.get_details_of_line_being_tested()

        if not lines or not lines[0].assertion_colrange or not line:
            self.view.erase_regions('current_syntax_test')
            return

        cursor = self.view.sel()[0]
        highlight_only_cursor = False
        if cursor.empty():
            cursor = sublime.Region(cursor.begin(), cursor.end() + 1)
        else:
            highlight_only_cursor = re.match(r'^\^+$', self.view.substr(cursor)) is not None

        col_start, col_end = lines[0].assertion_colrange
        if highlight_only_cursor:
            col_start = self.view.rowcol(cursor.begin())[1]
            col_end = self.view.rowcol(cursor.end())[1]
        elif col_end == col_start:
            col_end += 1

        # if the tests extend past the newline character, stop highlighting at the \n
        # as this is what these tests will assert against
        pos_start = min(line.begin() + col_start, line.end())
        pos_end = min(line.begin() + col_end, line.end() + 1)
        region = sublime.Region(pos_start, pos_end)

        scope = get_setting('syntax_test.highlight_scope', 'text')
        styles = get_setting('syntax_test.highlight_styles', ['DRAW_NO_FILL'])
        style_flags = RegionOption(*styles)

        self.view.add_regions('current_syntax_test', [region], scope, '', style_flags)

    def on_query_context(self, key, operator, operand, match_all):
        """Respond to relevant syntax test keybinding contexts."""

        view = self.view
        # all contexts supported will have boolean results, so ignore regex operators
        if operator not in (sublime.OP_EQUAL, sublime.OP_NOT_EQUAL):
            return None

        def current_line_is_a_syntax_test():
            results = (self.is_syntax_test_line(reg.begin(), False)
                       for reg in view.sel())
            aggregator = all if match_all else any
            return aggregator(results)

        keys = {
            "current_line_is_a_syntax_test": current_line_is_a_syntax_test,
            "file_contains_syntax_tests": lambda: bool(self.header),
        }

        if key not in keys:
            return None
        else:
            result = keys[key]() == bool(operand)
            if operator == sublime.OP_NOT_EQUAL:
                result = not result
            return result


def find_common_scopes(scopes, skip_syntax_suffix):
    """Find the (partial) scopes that are common for a list of scopes.

    Example of unique scopes for the following Python code (and test positions):

    def function():
    ^^^^^^^^^^^^^^
    [
      'source.python meta.function.python storage.type.function.python',
      'source.python meta.function.python',
      'source.python meta.function.python entity.name.function.python',
      'source.python meta.function.parameters.python punctuation.section.parameters.begin.python'
    ]

    The common scope for these, ignoring the base scope, will be 'meta.function'
    """

    # we will use the scopes from index 0 and test against the scopes from the further indexes
    # as any scopes that doesn't appear in this index aren't worth checking, they can't be common

    # skip the base scope i.e. `source.python`
    check_scopes = next(iter(scopes)).split()[1:]

    shared_scopes = ''
    # stop as soon as at least one shared scope was found
    # or when there are no partial scopes left to check
    while not shared_scopes and check_scopes:
        if not skip_syntax_suffix:
            for check_scope in check_scopes:
                # check that the scope matches when combined with the shared scopes that have
                # already been discovered, because order matters (a space in a scope selector
                # is an operator, meaning the next scope must appear somewhere to the right
                # of the one before), and some scopes may appear more than once
                compare_with = shared_scopes + check_scope
                if all(sublime.score_selector(scope, compare_with) > 0 for scope in scopes):
                    shared_scopes += check_scope + ' '

        # if no matches were found
        if not shared_scopes:
            # split off the last partial scope from each scope to check
            # i.e. `meta.function.parameters` becomes `meta.function`
            # if the scope to check doesn't contain any sub-scopes i.e. `meta`,
            # then drop it from the list of scopes to check
            check_scopes = [
                '.'.join(check_scope.split('.')[0:-1])
                for check_scope in check_scopes if '.' in check_scope
            ]
            skip_syntax_suffix = False

    return shared_scopes.strip()


class PackagedevAlignSyntaxTestCommand(sublime_plugin.TextCommand):

    """Align the cursor with spaces to be to the right of the previous line's assertion."""

    def run(self, edit):
        view = self.view
        cursor = view.sel()[0]

        listener = sublime_plugin.find_view_event_listener(view, SyntaxTestHighlighterListener)
        if not listener:
            return

        details = listener.get_details_of_test_assertion_line(cursor.begin())
        if not details.comment_marker_match:
            return

        # find the last test assertion column on the previous line
        details = listener.get_details_of_test_assertion_line(details.line_region.begin() - 1)
        next_col = None
        skip_whitespace = get_setting('syntax_test.skip_whitespace', True)
        if details.assertion_colrange:
            next_col = details.assertion_colrange[1]
        else:
            # the previous line wasn't a syntax test line, so instead
            # start at the first position on the line. We will then
            # advance to the first non-whitespace char on the line.
            next_col = 0
            skip_whitespace = True

        # find the next non-whitespace char on the line being tested above
        if skip_whitespace:
            line_region = listener.get_details_of_line_being_tested()[1]
            pos = line_region.begin() + next_col
            for pos in range(pos, line_region.end()):
                if view.substr(pos).strip() != '':
                    break
            next_col = view.rowcol(pos)[1]

        col_diff = next_col - view.rowcol(cursor.begin())[1]
        view.insert(edit, cursor.end(), " " * col_diff)
        view.run_command('packagedev_suggest_syntax_test')


class PackagedevSuggestSyntaxTestCommand(sublime_plugin.TextCommand):
    """Intelligently suggest where the syntax test assertions should be placed.

    This is based on the scopes of the line being tested, and where they change.
    """

    def run(self, edit, character='^'):
        """Available parameters:
        edit (sublime.Edit)
            The edit parameter from TextCommand.
        character (str) = '^'
            The character to insert when suggesting where the test assertions should go.
        """

        view = self.view
        view.replace(edit, view.sel()[0], character)
        insert_at = view.sel()[0].begin()
        _, col = view.rowcol(insert_at)

        listener = sublime_plugin.find_view_event_listener(view, SyntaxTestHighlighterListener)
        if not listener or not listener.header:
            return

        lines, line = listener.get_details_of_line_being_tested()
        if not lines[-1].assertion_colrange:
            return
        end_token = listener.header.comment_end
        # don't duplicate the end token if it is on the line but not selected
        if end_token and view.sel()[0].end() == lines[0].line_region.end():
            end_token = ' ' + end_token
        else:
            end_token = ''

        if character == '-':
            length = 1
            scopes = {view.scope_name(line.begin() + lines[0].assertion_colrange[0])}
        elif character == '^':
            length, scopes = self.determine_test_extends(lines, line, col)
        else:
            return

        suggest_suffix = get_setting('syntax_test.suggest_scope_suffix', True)
        scope = find_common_scopes(scopes, not suggest_suffix)

        trim_prefix = not get_setting('syntax_test.suggest_asserted_prefix', False)
        if trim_prefix:
            scopes_above = [
                self.view.substr(sublime.Region(
                    line.line_region.begin() + line.assertion_colrange[1],
                    line.line_region.end(),
                )).strip()
                for line in lines[1:]
                if line.assertion_colrange[0] <= lines[0].assertion_colrange[0]
                and line.assertion_colrange[1] >= lines[0].assertion_colrange[1]
            ]

            for scope_above in reversed(scopes_above):
                # Determine the last scope segment matched by any previous assertion
                # and trim that and everything preceding it.
                score = sublime.score_selector(scope, scope_above)
                if score > 0:
                    scope_parts = scope.split(' ')
                    matched_count = -(-score.bit_length() // 3) - 1

                    score_of_last_part = score >> matched_count * 3
                    possible_score_of_last_part = len(scope_parts[matched_count - 1].split('.'))
                    if score_of_last_part != possible_score_of_last_part:
                        matched_count -= 1

                    scope = ' '.join(scope_parts[matched_count:])

        # delete the existing selection
        if not view.sel()[0].empty():
            view.erase(edit, view.sel()[0])

        view.insert(edit, insert_at, (character * max(1, length)) + ' ' + scope + end_token)

        # move the selection to cover the added scope name,
        # so that the user can easily insert another ^ to extend the test
        view.sel().clear()
        view.sel().add(sublime.Region(
            insert_at + length,
            insert_at + length + len(' ' + scope + end_token)
        ))

    def determine_test_extends(self, lines, line, start_col):
        """Determine extend of token(s) to test and return lenght and scope set.

        To be precise, increase column as long as the selector wouldn't change
        and collect the scopes.
        """
        view = self.view
        col_start, col_end = lines[0].assertion_colrange
        scopes = {
            view.scope_name(pos)
            for pos in range(line.begin() + col_start, line.begin() + col_end)
        }
        base_scope = path.commonprefix(list(scopes)).strip()
        logger.debug("Original base scope: %r", base_scope)

        length = 0
        for pos in range(line.begin() + col_end - 1, line.end() + 1):
            scope = view.scope_name(pos)
            if scope.startswith(base_scope):
                scopes.add(scope)
                length += 1
            else:
                break

        logger.debug("Total scopes covered: %r", scopes)
        return length, scopes


class AssignSyntaxTestSyntaxListener(sublime_plugin.EventListener):

    """Assign target syntax highlighting to a syntax test file."""

    PLAIN_TEXT = "Packages/Text/Plain text.tmLanguage"

    def on_load(self, view):
        file_name = view.file_name()
        if not file_name:
            return
        file_path = Path(file_name)
        if (
            not file_path.name.startswith("syntax_test_")
            or not path_is_relative_to(file_path, sublime.packages_path())
        ):
            return

        if view.size() == 0:
            logger.debug("Delaying on_load because view was empty")
            sublime.set_timeout(lambda: self._on_load(view), 100)
        else:
            self._on_load(view)

    def _on_load(self, view):
        self.assign_syntax(view)
        self.check_for_tabs(view)

    def assign_syntax(self, view):
        test_header = get_syntax_test_tokens(view)
        if not test_header:
            return
        current_syntax = view.settings().get('syntax', None)
        test_syntax = test_header.syntax_file

        if current_syntax == test_syntax:
            return

        # resource path specified
        elif "/" in test_syntax:
            *_, file_name = test_syntax.rpartition("/")
            if test_syntax in sublime.find_resources(file_name):
                view.assign_syntax(test_syntax)
            else:  # file doesn't exist
                logger.info("Couldn't find a file at %r", test_syntax)
                view.assign_syntax(self.PLAIN_TEXT)

        # just base name specified
        elif not current_syntax.endswith('/' + test_syntax):
            syntax_candidates = sublime.find_resources(test_syntax)
            if syntax_candidates:
                logger.debug("Found the following candidates for %r: %r",
                             test_syntax, syntax_candidates)
                view.assign_syntax(syntax_candidates[0])
            else:
                logger.info("Couldn't find a syntax matching %r", test_syntax)
                view.assign_syntax(self.PLAIN_TEXT)

    def check_for_tabs(self, view):
        if not view.is_valid():
            return

        if view.settings().get('translate_tabs_to_spaces', False):
            return

        sheet = view.sheet()
        if sheet.is_transient() or sheet.is_semi_transient():
            # View was opened in the background by e.g. Go To Anything,
            # so we wait for the view to be opened completely.
            sublime.set_timeout(lambda: self.check_for_tabs(view), 100)
            return

        # offer user to fix settings if they try to do something stupid
        if sublime.ok_cancel_dialog(
            (
                "This view is configured to use tabs for indentation. "
                "Syntax tests do not work properly with tabs.\n"
                "Do you want to change this view's settings to use spaces?\n"
                "Note that existing tab characters are NOT automatically converted!"
            ),
            "Change setting"
        ):
            view.settings().set('translate_tabs_to_spaces', True)

    on_post_save_async = on_load


class ScopeTreeNode:

    def __init__(self, region, scope, children=None):
        self.region = region
        self.scope = scope
        self.children = children or []

    @classmethod
    def build_forest(cls, tokens, *, trim_suffix=False):
        tokens = [
            (region, cls._split_scope(scope, trim_suffix=trim_suffix))
            for region, scope in tokens
        ]

        forest = []
        for region, scope in tokens:
            cls._insert(forest, region, scope)

        return [node.compacted() for node in forest]

    @staticmethod
    def _split_scope(scope, *, trim_suffix=False):
        ret = scope.strip().split(' ')
        ret = ret[1:]  # Trim root scope
        if trim_suffix:
            ret = [
                '.'.join(part.split('.')[:-1])
                for part in ret
            ]
        return ret

    @classmethod
    def _insert(cls, forest, region, scopes):
        if scopes:
            first, *rest = scopes
            if forest and forest[-1].scope == first and forest[-1].region.b == region.a:
                forest[-1].region = forest[-1].region.cover(region)
            else:
                forest.append(cls(region, first))

            cls._insert(forest[-1].children, region, rest)

    def compacted(self):
        if (
            len(self.children) == 1
            and self.children[0].region.to_tuple() == self.region.to_tuple()
        ):
            replacement_node = self.children[0].compacted()
            replacement_node.scope = self.scope + ' ' + replacement_node.scope
            return replacement_node
        else:
            return ScopeTreeNode(
                self.region,
                self.scope,
                [node.compacted() for node in self.children],
            )

    def __repr__(self):
        from pprint import pformat
        from textwrap import indent
        return "ScopeTreeNode(\n\t{!r},\n\t{!r},\n{}\n)".format(
            self.region,
            self.scope,
            indent(pformat(self.children), "\t"),
        )


class PackagedevGenerateSyntaxTestsForLineCommand(sublime_plugin.TextCommand):

    """Generate syntax tests for the selected line of code."""

    def is_enabled(self):
        listener = sublime_plugin.find_view_event_listener(
            self.view,
            SyntaxTestHighlighterListener,
        )
        return bool(listener and listener.header)

    def run(self, edit):
        view = self.view
        listener = sublime_plugin.find_view_event_listener(view, SyntaxTestHighlighterListener)
        if not listener or not listener.header:
            return

        suggest_suffix = get_setting('syntax_test.suggest_scope_suffix', True)

        for region in reversed(view.sel()):
            line = view.line(region.b)

            forest = ScopeTreeNode.build_forest(
                view.extract_tokens_with_scopes(line),
                trim_suffix=not suggest_suffix,
            )

            tests = self.get_test_lines(forest, listener.header, line.begin())

            view.insert(edit, line.end(), ''.join(tests))

    def get_test_lines(self, forest, header, line_start):
        comment_start = header.comment_start
        comment_start_len = len(comment_start)

        if header.comment_end is None:
            comment_end = ''
        else:
            comment_end = ' ' + header.comment_end

        def recurse(forest):
            for child in forest:
                range_start = max(child.region.begin() - line_start, comment_start_len)
                range_end = child.region.end() - line_start

                if range_end > range_start:
                    yield "\n{comment_start}{space}{range} {scope}{comment_end}".format(
                        comment_start=comment_start,
                        space=' ' * (range_start - comment_start_len),
                        range='^' * (range_end - range_start),
                        scope=child.scope,
                        comment_end=comment_end,
                    )

                yield from recurse(child.children)

        return list(recurse(forest))
