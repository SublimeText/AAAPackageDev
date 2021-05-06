from collections import OrderedDict, namedtuple
import logging
import re

import sublime
import sublime_plugin

from sublime_lib import ResourcePath

from .lib.scope_data import completions_from_prefix
from .lib import syntax_paths
from .lib import inhibit_word_completions

__all__ = (
    'ColorSchemeCompletionsListener',
    'PackagedevEditSchemeCommand',
)

SCHEME_TEMPLATE = """\
{
  // http://www.sublimetext.com/docs/3/color_schemes.html
  "variables": {
    // "green": "#FF0000",
  },
  "globals": {
    // "foreground": "var(green)",
  },
  "rules": [
    {
      // "scope": "string",
      // "foreground": "#00FF00",
    },
  ],
}""".replace("  ", "\t")

KIND_SCHEME = (sublime.KIND_ID_VARIABLE, "s", "Scheme")
KIND_VARIABLE = (sublime.KIND_ID_VARIABLE, 'v', 'Variable')

BUILTIN_VARIABLES = [
    sublime.CompletionItem(
        trigger=var,
        annotation="built-in",
        completion=var,
        kind=KIND_VARIABLE,
    )
    for var in (
        "--background",
        "--foreground",
        "--accent",
        "--bluish",
        "--cyanish",
        "--greenish",
        "--orangish",
        "--pinkish",
        "--purplish",
        "--redish",
        "--yellowish",
    )
]

# Copied from 'Default/ui.py'
DEFAULT_CS = 'Packages/Color Scheme - Default/Mariana.sublime-color-scheme'

logger = logging.getLogger(__name__)


def _escape_in_snippet(v):
    return v.replace("}", "\\}").replace("$", "\\$")


class Variable(namedtuple("_Varible", "name value source")):
    def __eq__(self, other):
        return self.name == other.name

    def __hash__(self):
        return hash(self.name)

    def as_completion(self, with_key=False):
        if with_key:
            # TODO doesn't escape json chars
            formatted_value = _escape_in_snippet(sublime.encode_value(self.value))
            contents = '"{}": ${{0:{}}},'.format(self.name, formatted_value)
            completion_format = sublime.COMPLETION_FORMAT_SNIPPET
        else:
            contents = self.name
            completion_format = sublime.COMPLETION_FORMAT_TEXT

        return sublime.CompletionItem(
            trigger=self.name,
            annotation=self.source,
            completion=contents,
            completion_format=completion_format,
            kind=KIND_VARIABLE,
        )


def _collect_inherited_variables(name=None, extends=None, excludes=set()):
    """Collect inherited variables from overridden and extended theme files.

    Builds a Set[Variable] hashed on the variable's name
    and goes through files in FILO order
    while collecting the variables' value
    and which file they were found in.
    The result is a collection of variables that follows
    ST's internal override algorithm.
    """
    extends = extends or OrderedDict()  # preserve order & deduplicate
    if name:
        resources = (r for r in reversed(sublime.find_resources(name)) if r not in excludes)
        for resource in resources:
            try:
                contents = sublime.decode_value(sublime.load_resource(resource))
                if isinstance(contents, list):
                    logger.debug("Skipping old-style theme '%s'", resource)
                    continue
                if 'variables' in contents:
                    for k, v in contents['variables'].items():
                        yield Variable(k, v, name)
                if 'extends' in contents:
                    extends[contents['extends']] = True
            except Exception as e:
                logger.error("Unable to read variables in '%s' [%s]", resource, e)

    for extended_name in extends.keys():
        logger.debug("Recursing into extended theme '%s'", extended_name)
        yield from _collect_inherited_variables(extended_name, excludes=excludes)


class ColorSchemeCompletionsListener(sublime_plugin.ViewEventListener):

    """Provide variable and scope name completions for color schemes.

    Extract completions from defined variables in the current file
    and determine scope completions based on our scope_data module.

    Also provide variable completions for themes.
    """

    @classmethod
    def applies_to_primary_view_only(cls):
        return False

    @classmethod
    def is_applicable(cls, settings):
        return settings.get('syntax') in (syntax_paths.COLOR_SCHEME, syntax_paths.THEME)

    def _line_prefix(self, point):
        _, col = self.view.rowcol(point)
        line = self.view.substr(self.view.line(point))
        return line[:col]

    def _inherited_variables(self):
        """Wraps _collect_inherited_variables for the current view."""
        name, extends, excludes = None, OrderedDict(), set()
        if self.view.file_name():
            this_resource = ResourcePath.from_file_path(self.view.file_name())
            name = this_resource.name
            excludes.add(str(this_resource))

        extends_regions = self.view.find_by_selector("meta.extends.sublime-theme")
        if extends_regions:
            extended_name = sublime.decode_value(self.view.substr(extends_regions.pop()))
            extends[extended_name] = True
            if extends_regions:
                logger.warning('Found more than 1 "extends" key for theme')

        return set(_collect_inherited_variables(name, extends, excludes))

    def variable_completions(self, locations):
        variable_regions = self.view.find_by_selector("entity.name.variable.sublime-color-scheme"
                                                      "| entity.name.variable.sublime-theme")
        variables = {Variable(self.view.substr(r), None, "") for r in variable_regions}
        inherited_variables = self._inherited_variables()
        variables |= inherited_variables
        logger.debug("Found %d (%d inherited) variables to complete: %r",
                     len(variables), len(inherited_variables), variables)
        variable_completions = [var.as_completion() for var in variables]

        if self.view.match_selector(locations[0], "source.json.sublime.theme"):
            variable_completions += BUILTIN_VARIABLES

        return variable_completions

    def variable_definition_completions(self, with_key=False):
        variables = self._inherited_variables()
        if not variables:
            return None
        sorted_variables = sorted(variables)
        logger.debug("Found %d inherited variables to complete: %r",
                     len(variables), sorted_variables)
        return [var.as_completion(with_key) for var in sorted_variables]

    def _scope_prefix(self, locations):
        # Determine entire prefix
        prefixes = set()
        for point in locations:
            text = self._line_prefix(point)
            real_prefix = re.search(r'[\w.-]*$', text).group(0)  # may be zero-length
            prefixes.add(real_prefix)

        if len(prefixes) > 1:
            return None
        else:
            return next(iter(prefixes))

    def scope_completions(self, locations):
        real_prefix = self._scope_prefix(locations)
        logger.debug("Full prefix: %r", real_prefix)
        if real_prefix is None:
            return None
        else:
            return completions_from_prefix(real_prefix)

    @inhibit_word_completions
    def on_query_completions(self, prefix, locations):

        def verify_scope(selector, offset=0):
            """Verify scope for each location."""
            return all(self.view.match_selector(point + offset, selector)
                       for point in locations)

        if (
            verify_scope("meta.function-call.var.sublime-color-scheme")
            or (verify_scope("meta.function-call.var.sublime-color-scheme", -1)
                and verify_scope("punctuation.definition.string.end.json"))
        ):
            return self.variable_completions(locations)

        elif verify_scope("meta.scope-selector.sublime"):
            return self.scope_completions(locations)

        elif verify_scope("meta.variable-name"):
            return self.variable_definition_completions()

        elif verify_scope("meta.variables - string - comment"):
            return self.variable_definition_completions(with_key=True)

        else:
            return None


class PackagedevEditSchemeCommand(sublime_plugin.WindowCommand):

    """Like syntax-specific settings but for the currently used color scheme."""

    def run(self):
        view = self.window.active_view()
        if not view:
            return

        scheme_path = self.get_scheme_path(view, 'color_scheme')
        if scheme_path != 'auto':
            self.open_scheme(scheme_path)
        else:
            paths = [
                (setting, self.get_scheme_path(view, setting))
                for setting in ('dark_color_scheme', 'light_color_scheme')
            ]
            current_os_mode = sublime.ui_info()['system']['style']
            choices = [
                sublime.QuickPanelItem(setting, details=str(path), kind=KIND_SCHEME)
                for setting, path in paths
            ]

            selected_index = -1
            for idx, choice in enumerate(choices):
                if choice.trigger.startswith(current_os_mode):
                    choice.annotation = 'Active'
                    selected_index = idx

            def on_done(i):
                if i >= 0:
                    self.open_scheme(paths[i][1])

            self.window.show_quick_panel(
                choices,
                on_done,
                selected_index=selected_index,
                placeholder='Choose a color scheme to edit ...'
            )

    @staticmethod
    def get_scheme_path(view, setting_name):
        # Be lazy here and don't consider invalid values
        scheme_setting = view.settings().get(setting_name, DEFAULT_CS)
        if scheme_setting == 'auto':
            return 'auto'
        elif "/" not in scheme_setting:
            return ResourcePath.glob_resources(scheme_setting)[0]
        else:
            return ResourcePath(scheme_setting)

    def open_scheme(self, scheme_path):
        # theme_path = ResourcePath(sublime.find_resources(scheme_path)[0])
        self.window.run_command(
            'edit_settings',
            {
                'base_file': '/'.join(("${packages}",) + scheme_path.parts[1:]),
                'user_file': "${packages}/User/" + scheme_path.stem + '.sublime-color-scheme',
                'default': SCHEME_TEMPLATE,
            },
        )
