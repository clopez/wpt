"""Generates Canvas tests from YAML file definitions."""
# Current code status:
#
# This was originally written by Philip Taylor for use at
# http://philip.html5.org/tests/canvas/suite/tests/
#
# It has been adapted for use with the Web Platform Test Suite suite at
# https://github.com/web-platform-tests/wpt/
#
# The original version had a number of now-removed features (multiple versions
# of each test case of varying verbosity, Mozilla mochitests, semi-automated
# test harness). It also had a different directory structure.

# To update or add test cases:
#
# * Modify the tests*.yaml files.
#  - 'name' is an arbitrary hierarchical name to help categorise tests.
#  - 'desc' is a rough description of what behaviour the test aims to test.
#  - 'code' is JavaScript code to execute, with some special commands starting
#    with '@'.
#  - 'expected' is what the final canvas output should be: a string 'green' or
#    'clear' (100x50 images in both cases), or a string 'size 100 50' (or any
#    other size) followed by Python code using Pycairo to generate the image.
#
# * Run "./build.sh".
# This requires a few Python modules which might not be ubiquitous.
# It will usually emit some warnings, which ideally should be fixed but can
# generally be safely ignored.
#
# * Test the tests, add new ones to Git, remove deleted ones from Git, etc.

from typing import Any, Container, DefaultDict, FrozenSet, List, Mapping
from typing import MutableMapping, Set, Union

import re
import collections
import copy
import dataclasses
import enum
import importlib
import math
import os
import pathlib
import sys
import textwrap

import jinja2

try:
    import cairocffi as cairo  # type: ignore
except ImportError:
    import cairo

try:
    # Compatible and lots faster.
    import syck as yaml  # type: ignore
except ImportError:
    import yaml


class Error(Exception):
    """Base class for all exceptions raised by this module"""


class InvalidTestDefinitionError(Error):
    """Raised on invalid test definition."""


def _double_quote_escape(string: str) -> str:
    return string.replace('\\', '\\\\').replace('"', '\\"')


def _escape_js(string: str) -> str:
    string = _double_quote_escape(string)
    # Kind of an ugly hack, for nicer failure-message output.
    string = re.sub(r'\[(\w+)\]', r'[\\""+(\1)+"\\"]', string)
    return string


def _expand_nonfinite(method: str, argstr: str, tail: str) -> str:
    """
    >>> print _expand_nonfinite('f', '<0 a>, <0 b>', ';')
    f(a, 0);
    f(0, b);
    f(a, b);
    >>> print _expand_nonfinite('f', '<0 a>, <0 b c>, <0 d>', ';')
    f(a, 0, 0);
    f(0, b, 0);
    f(0, c, 0);
    f(0, 0, d);
    f(a, b, 0);
    f(a, b, d);
    f(a, 0, d);
    f(0, b, d);
    """
    # argstr is "<valid-1 invalid1-1 invalid2-1 ...>, ..." (where usually
    # 'invalid' is Infinity/-Infinity/NaN).
    args = []
    for arg in argstr.split(', '):
        match = re.match('<(.*)>', arg)
        if match is None:
            raise InvalidTestDefinitionError(
                f'Expected arg to match format "<(.*)>", but was: {arg}')
        a = match.group(1)
        args.append(a.split(' '))
    calls = []
    # Start with the valid argument list.
    call = [args[j][0] for j in range(len(args))]
    # For each argument alone, try setting it to all its invalid values:
    for i, arg in enumerate(args):
        for a in arg[1:]:
            c2 = call[:]
            c2[i] = a
            calls.append(c2)
    # For all combinations of >= 2 arguments, try setting them to their
    # first invalid values. (Don't do all invalid values, because the
    # number of combinations explodes.)
    def f(c: List[str], start: int, depth: int) -> None:
        for i in range(start, len(args)):
            if len(args[i]) > 1:
                a = args[i][1]
                c2 = c[:]
                c2[i] = a
                if depth > 0:
                    calls.append(c2)
                f(c2, i + 1, depth + 1)

    f(call, 0, 0)

    str_calls = (', '.join(c) for c in calls)
    return '\n'.join(f'{method}({params}){tail}' for params in str_calls)


def _get_test_sub_dir(name: str, name_to_sub_dir: Mapping[str, str]) -> str:
    for prefix in sorted(name_to_sub_dir.keys(), key=len, reverse=True):
        if name.startswith(prefix):
            return name_to_sub_dir[prefix]
    raise InvalidTestDefinitionError(
        f'Test "{name}" has no defined target directory mapping')


def _remove_extra_newlines(text: str) -> str:
    """Remove newlines if a backslash is found at end of line."""
    # Lines ending with '\' gets their newline character removed.
    text = re.sub(r'\\\n', '', text, flags=re.MULTILINE | re.DOTALL)

    # Lines ending with '\-' gets their newline and any leading white spaces on
    # the following line removed.
    text = re.sub(r'\\-\n\s*', '', text, flags=re.MULTILINE | re.DOTALL)
    return text


def _expand_test_code(code: str) -> str:
    code = re.sub(r' @moz-todo', '', code)

    code = re.sub(r'@moz-UniversalBrowserRead;', '', code)

    code = re.sub(r'@nonfinite ([^(]+)\(([^)]+)\)(.*)', lambda m:
                  _expand_nonfinite(m.group(1), m.group(2), m.group(3)),
                  code)  # Must come before '@assert throws'.

    code = re.sub(r'@assert pixel (\d+,\d+) == (\d+,\d+,\d+,\d+);',
                  r'_assertPixel(canvas, \1, \2);', code)

    code = re.sub(r'@assert pixel (\d+,\d+) ==~ (\d+,\d+,\d+,\d+);',
                  r'_assertPixelApprox(canvas, \1, \2, 2);', code)

    code = re.sub(r'@assert pixel (\d+,\d+) ==~ (\d+,\d+,\d+,\d+) \+/- (\d+);',
                  r'_assertPixelApprox(canvas, \1, \2, \3);', code)

    code = re.sub(r'@assert throws (\S+_ERR) (.*?);$',
                  r'assert_throws_dom("\1", function() { \2; });', code,
                  flags=re.MULTILINE | re.DOTALL)

    code = re.sub(r'@assert throws (\S+Error) (.*?);$',
                  r'assert_throws_js(\1, function() { \2; });', code,
                  flags=re.MULTILINE | re.DOTALL)

    code = re.sub(
        r'@assert (.*) === (.*);', lambda m:
        (f'_assertSame({m.group(1)}, {m.group(2)}, '
         f'"{_escape_js(m.group(1))}", "{_escape_js(m.group(2))}");'), code)

    code = re.sub(
        r'@assert (.*) !== (.*);', lambda m:
        (f'_assertDifferent({m.group(1)}, {m.group(2)}, '
         f'"{_escape_js(m.group(1))}", "{_escape_js(m.group(2))}");'), code)

    code = re.sub(
        r'@assert (.*) =~ (.*);',
        lambda m: f'assert_regexp_match({m.group(1)}, {m.group(2)});', code)

    code = re.sub(
        r'@assert (.*);',
        lambda m: f'_assert({m.group(1)}, "{_escape_js(m.group(1))}");', code)

    assert '@' not in code

    return code


_TestParams = Mapping[str, Any]
_MutableTestParams = MutableMapping[str, Any]


class _CanvasType(str, enum.Enum):
    HTML_CANVAS = 'HtmlCanvas'
    OFFSCREEN_CANVAS = 'OffscreenCanvas'
    WORKER = 'Worker'


class _TemplateType(str, enum.Enum):
    REFERENCE = 'reference'
    HTML_REFERENCE = 'html_reference'
    CAIRO_REFERENCE = 'cairo_reference'
    TESTHARNESS = 'testharness'


@dataclasses.dataclass
class _OutputPaths:
    element: pathlib.Path
    offscreen: pathlib.Path

    def sub_path(self, sub_dir: str):
        """Create a new _OutputPaths that is a subpath of this _OutputPath."""
        return _OutputPaths(element=self.element / sub_dir,
                            offscreen=self.offscreen / sub_dir)

    def mkdir(self) -> None:
        """Creates element and offscreen directories, if they don't exist."""
        self.element.mkdir(parents=True, exist_ok=True)
        self.offscreen.mkdir(parents=True, exist_ok=True)


def _validate_test(test: _TestParams):
    if test.get('expected', '') == 'green' and re.search(
            r'@assert pixel .* 0,0,0,0;', test['code']):
        print(f'Probable incorrect pixel test in {test["name"]}')

    if 'size' in test and (not isinstance(test['size'], tuple)
                           or len(test['size']) != 2):
        raise InvalidTestDefinitionError(
            f'Invalid canvas size "{test["size"]}" in test {test["name"]}. '
            'Expected an array with two numbers.')

    if test['template_type'] == _TemplateType.TESTHARNESS:
        valid_test_types = {'sync', 'async', 'promise'}
    else:
        valid_test_types = {'promise'}

    test_type = test.get('test_type')
    if test_type is not None and test_type not in valid_test_types:
        raise InvalidTestDefinitionError(
            f'Invalid test_type: {test_type}. '
            f'Valid values are: {valid_test_types}.')


def _render_template(jinja_env: jinja2.Environment, template: jinja2.Template,
                     params: _TestParams) -> str:
    """Renders the specified jinja template.

    The template is repetitively rendered until no more changes are observed.
    This allows for template parameters to refer to other template parameters.
    """
    rendered = template.render(params)
    previous = ''
    while rendered != previous and ('{{' in rendered or '{%' in rendered):
        previous = rendered
        template = jinja_env.from_string(rendered)
        rendered = template.render(params)
    return rendered


def _render(jinja_env: jinja2.Environment, template_name: str,
            params: _TestParams, output_file_name: str):
    template = jinja_env.get_template(template_name)
    file_content = _render_template(jinja_env, template, params)
    pathlib.Path(output_file_name).write_text(file_content, 'utf-8')


def _preprocess_code(jinja_env: jinja2.Environment, code: str,
                     params: _TestParams) -> str:
    code = _remove_extra_newlines(code)

    # Render the code on its own, as it could contain templates expanding
    # to multiple lines. This is needed to get proper indentation of the
    # code in the main template.
    if '{{' in code or '{%' in code:
        code = _render_template(jinja_env, jinja_env.from_string(code), params)

    # Expand "@..." macros.
    code = _expand_test_code(code)
    return code


def _write_cairo_images(pycairo_code: str, output_files: _OutputPaths,
                        canvas_types: FrozenSet[_CanvasType]) -> None:
    """Creates a png from pycairo code, for the specified canvas types."""
    if _CanvasType.HTML_CANVAS in canvas_types:
        full_code = (f'{pycairo_code}\n'
                     f'surface.write_to_png("{output_files.element}")\n')
        eval(compile(full_code, '<string>', 'exec'), {
            'cairo': cairo,
            'math': math,
        })

    if {_CanvasType.OFFSCREEN_CANVAS, _CanvasType.WORKER} & canvas_types:
        full_code = (f'{pycairo_code}\n'
                     f'surface.write_to_png("{output_files.offscreen}")\n')
        eval(compile(full_code, '<string>', 'exec'), {
            'cairo': cairo,
            'math': math,
        })


class _Variant():

    def __init__(self, params: _MutableTestParams) -> None:
        # Raw parameters, as specified in YAML, defining this test variant.
        self._params = params  # type: _MutableTestParams
        # Parameters rendered for each enabled canvas types.
        self._canvas_type_params = {
            }  # type: MutableMapping[_CanvasType, _MutableTestParams]

    @property
    def params(self) -> _MutableTestParams:
        """Returns this variant's raw param dict, as it's defined in YAML."""
        return self._params

    @property
    def canvas_type_params(self) -> MutableMapping[_CanvasType,
                                                   _MutableTestParams]:
        """Returns this variant's param dict for different canvas types."""
        return self._canvas_type_params

    @staticmethod
    def create_with_defaults(test: _TestParams) -> '_Variant':
        """Create a _Variant from the specified params.

        Default values are added for certain parameters, if missing."""
        params = {
            'desc': '',
            'size': (100, 50),
            # Test name, which ultimately is used as filename. File variant
            # dimension names (i.e. the 'file_variant_names' property below) are
            # appended to this to produce unique filenames.
            'name': '',
            # List holding the the file variant dimension names.
            'file_variant_names': [],
            # List of this variant grid dimension names. This uniquely
            # identifies a single variant in a variant grid file.
            'grid_variant_names': [],
            # List of this variant dimension names, including both file and grid
            # dimensions.
            'variant_names': [],
            # Same as `file_variant_names`, but concatenated into a single
            # string. This is a useful to easily identify a variant file.
            'file_variant_name': '',
            # Same as `grid_variant_names`, but concatenated into a single
            # string. This is a useful to easily identify a variant in a grid.
            'grid_variant_name': '',
            # Same as `variant_names`, but concatenated into a single string.
            # This is a useful shorthand for tests having a single variant
            # dimension.
            'variant_name': '',
            'images': [],
            'svgimages': [],
            'fonts': [],
        }
        params.update(test)
        return _Variant(params)

    def merge_params(self, params: _TestParams) -> '_Variant':
        """Returns a new `_Variant` that merges `self.params` and `params`."""
        new_params = copy.deepcopy(self._params)
        new_params.update(params)
        return _Variant(new_params)

    def _add_variant_name(self, name: str) -> None:
        self._params['variant_name'] += (
            ('.' if self.params['variant_name'] else '') + name)
        self._params['variant_names'] += [name]

    def with_grid_variant_name(self, name: str) -> '_Variant':
        """Addend a variant name to include in the grid element label."""
        self._add_variant_name(name)
        self._params['grid_variant_name'] += (
            ('.' if self.params['grid_variant_name'] else '') + name)
        self._params['grid_variant_names'] += [name]
        return self

    def with_file_variant_name(self, name: str) -> '_Variant':
        """Addend a variant name to include in the generated file name."""
        self._add_variant_name(name)
        self._params['file_variant_name'] += (
            ('.' if self.params['file_variant_name'] else '') + name)
        self._params['file_variant_names'] += [name]
        if self.params.get('append_variants_to_name', True):
            self._params['name'] += '.' + name
        return self

    def _render_param(self, jinja_env: jinja2.Environment,
                      param_name: str) -> None:
        """Render the specified parameter in-place in the `params` dict."""
        value = self.params.get(param_name)
        if value and isinstance(value, str) and ('{{' in value or
                                                 '{%' in value):
            self._params[param_name] = (
                jinja_env.from_string(value).render(self.params))


    def _get_file_name(self) -> str:
        file_name = self.params['name']

        if 'manual' in self.params:
            file_name += '-manual'

        return file_name

    def _get_canvas_types(self) -> FrozenSet[_CanvasType]:
        canvas_types = self.params.get('canvas_types', _CanvasType)
        invalid_types = {
            type
            for type in canvas_types if type not in list(_CanvasType)
        }
        if invalid_types:
            raise InvalidTestDefinitionError(
                f'Invalid canvas_types: {list(invalid_types)}. '
                f'Accepted values are: {[t.value for t in _CanvasType]}')
        return frozenset(_CanvasType(t) for t in canvas_types)

    def _get_template_type(self) -> _TemplateType:
        reference_types = (('reference' in self.params) +
                           ('html_reference' in self.params) +
                           ('cairo_reference' in self.params))
        if reference_types > 1:
            raise InvalidTestDefinitionError(
                f'Test {self.params["name"]} is invalid, only one of '
                '"reference", "html_reference" or "cairo_reference" can be '
                'specified at the same time.')

        if 'reference' in self.params:
            return _TemplateType.REFERENCE
        if 'html_reference' in self.params:
            return _TemplateType.HTML_REFERENCE
        if 'cairo_reference' in self.params:
            return _TemplateType.CAIRO_REFERENCE
        return _TemplateType.TESTHARNESS

    def finalize_params(self, jinja_env: jinja2.Environment,
                        variant_id: int) -> None:
        """Finalize this variant by adding computed param fields."""
        self._params['id'] = variant_id
        for param_name in ('attributes', 'desc', 'expected', 'name'):
            self._render_param(jinja_env, param_name)
        self._params['file_name'] = self._get_file_name()
        self._params['canvas_types'] = self._get_canvas_types()
        self._params['template_type'] = self._get_template_type()

        if isinstance(self._params['size'], list):
            self._params['size'] = tuple(self._params['size'])

        for canvas_type in self.params['canvas_types']:
            params = {'canvas_type': canvas_type}
            params.update(self._params)
            self._canvas_type_params[canvas_type] = params

            for name in ('code', 'reference', 'html_reference',
                         'cairo_reference'):
                param = params.get(name)
                if param is not None:
                    params[name] = _preprocess_code(jinja_env, param, params)

        _validate_test(self._params)

    def generate_expected_image(self, output_dirs: _OutputPaths) -> None:
        """Creates an expected image using Cairo and save filename in params."""
        # Expected images are only needed for HTML canvas tests.
        params = self._canvas_type_params.get(_CanvasType.HTML_CANVAS)
        if not params:
            return

        expected = params['expected']

        if expected == 'green':
            params['expected_img'] = '/images/green-100x50.png'
            return
        if expected == 'clear':
            params['expected_img'] = '/images/clear-100x50.png'
            return
        expected = re.sub(
            r'^size (\d+) (\d+)',
            r'surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, \1, \2)'
            r'\ncr = cairo.Context(surface)', expected)

        img_filename = f'{params["name"]}.png'
        _write_cairo_images(expected, output_dirs.sub_path(img_filename),
                            frozenset({_CanvasType.HTML_CANVAS}))
        params['expected_img'] = img_filename


class _VariantGrid:

    def __init__(self, variants: List[_Variant], grid_width: int) -> None:
        self._variants = variants
        self._grid_width = grid_width

        # Parameters rendered for each enabled canvas types.
        self._canvas_type_params = {
            }  # type: Mapping[_CanvasType, _MutableTestParams]
        self._file_name = None
        self._canvas_types = None
        self._template_type = None

    @property
    def variants(self) -> List[_Variant]:
        """Read only getter for the list of variant in this grid."""
        return self._variants

    @property
    def file_name(self) -> str:
        """File name to which this grid will be written."""
        if self._file_name is None:
            self._file_name = self._unique_param(_CanvasType, 'file_name')
        return self._file_name

    @property
    def canvas_types(self) -> FrozenSet[_CanvasType]:
        """Returns the set of all _CanvasType used by this grid's variants."""
        if self._canvas_types is None:
            self._canvas_types = self._param_set(_CanvasType, 'canvas_types')
        return self._canvas_types

    @property
    def template_type(self) -> _TemplateType:
        """Returns the type of Jinja template needed to render this grid."""
        if self._template_type is None:
            self._template_type = self._unique_param(_CanvasType,
                                                     'template_type')
        return self._template_type

    def finalize(self, jinja_env: jinja2.Environment):
        """Finalize this grid's variants, adding computed params fields."""
        for variant_id, variant in enumerate(self.variants):
            variant.finalize_params(jinja_env, variant_id)

        if len(self.variants) == 1:
            self._canvas_type_params = self.variants[0].canvas_type_params
        else:
            self._canvas_type_params = self._get_grid_params()

    def add_dimension(self, variants: Mapping[str,
                                              _TestParams]) -> '_VariantGrid':
        """Adds a variant dimension to this variant grid.

        If the grid currently has N variants, adding a dimension with M variants
        results in a grid containing N*M variants. Of course, we can't display
        more than 2 dimensions on a 2D screen, so adding dimensions beyond 2
        repeats all previous dimensions down vertically, with the grid width
        set to the number of variants of the first dimension (unless overridden
        by setting `grid_width`). For instance, a 3D variant space with
        dimensions 3 x 2 x 2 will result in this layout:
          000  100  200
          010  110  210

          001  101  201
          011  111  211
        """
        new_variants = [
            old_variant.merge_params(params or {}).with_grid_variant_name(name)
            for name, params in variants.items()
            for old_variant in self.variants
        ]
        # The first dimension dictates the grid-width, unless it was specified
        # beforehand via the test params.
        new_grid_width = (self._grid_width
                          if self._grid_width > 1 else len(variants))
        return _VariantGrid(variants=new_variants, grid_width=new_grid_width)

    def merge_params(self, name: str, params: _TestParams) -> '_VariantGrid':
        """Merges the specified `params` into every variant of this grid."""
        return _VariantGrid(variants=[
            variant.merge_params(params).with_file_variant_name(name)
            for variant in self.variants
        ],
                            grid_width=self._grid_width)

    def _variants_for_canvas_type(
            self, canvas_type: _CanvasType) -> List[_TestParams]:
        """Returns the variants of this grid enabled for `canvas_type`."""
        return [
            v.canvas_type_params[canvas_type]
            for v in self.variants
            if canvas_type in v.canvas_type_params
        ]

    def _unique_param(
            self, canvas_types: Container[_CanvasType], name: str) -> Any:
        """Returns the value of the `name` param for this grid.

        All the variants for all canvas types in `canvas_types` of this grid
        must agree on the same value for this parameter, or else an exception is
        thrown."""
        values = {params.get(name)
                  for variant in self.variants
                  for type, params in variant.canvas_type_params.items()
                  if type in canvas_types}
        if len(values) != 1:
            raise InvalidTestDefinitionError(
                'All variants in a variant grid must use the same value '
                f'for property "{name}". Got these values: {values}. '
                'Consider specifying the property outside of grid '
                'variants dimensions (in the base test definition or in a '
                'file variant dimension)')
        return values.pop()

    def _param_set(self, canvas_types: Container[_CanvasType], name: str):
        """Returns the set of all values this grid has for the `name` param.

        The `name` parameter of each variant is expected to be a sequence. These
        are all accumulated in a set and returned. The values are accumulated
        across all canvas types in `canvas_types`."""
        return frozenset(sum([list(params.get(name, []))
                              for v in self.variants
                              for type, params in v.canvas_type_params.items()
                              if type in canvas_types],
                             []))

    def _get_grid_params(self) -> Mapping[_CanvasType, _MutableTestParams]:
        """Returns the params dict needed to render this grid with Jinja."""
        grid_params = {}
        for canvas_type in self.canvas_types:
            params = grid_params[canvas_type] = {}
            params.update({
                'variants': self._variants_for_canvas_type(canvas_type),
                'grid_width': self._grid_width,
                'name': self._unique_param([canvas_type], 'name'),
                'test_type': self._unique_param([canvas_type], 'test_type'),
                'fuzzy': self._unique_param([canvas_type], 'fuzzy'),
                'timeout': self._unique_param([canvas_type], 'timeout'),
                'notes': self._unique_param([canvas_type], 'notes'),
                'images': self._param_set([canvas_type], 'images'),
                'svgimages': self._param_set([canvas_type], 'svgimages'),
                'fonts': self._param_set([canvas_type], 'fonts'),
            })
            if self.template_type in (_TemplateType.REFERENCE,
                                      _TemplateType.HTML_REFERENCE,
                                      _TemplateType.CAIRO_REFERENCE):
                params['desc'] = self._unique_param([canvas_type], 'desc')
        return grid_params

    def _write_reference_test(self, jinja_env: jinja2.Environment,
                              output_files: _OutputPaths):
        grid = '_grid' if len(self.variants) > 1 else ''

        # If variants don't all use the same offscreen and worker canvas types,
        # the offscreen and worker grids won't be identical. The worker test
        # therefore can't reuse the offscreen reference file.
        offscreen_types = {_CanvasType.OFFSCREEN_CANVAS, _CanvasType.WORKER}
        needs_worker_reference = len({
            variant.params['canvas_types'] & offscreen_types
            for variant in self.variants
        }) != 1

        test_templates = {
            _CanvasType.HTML_CANVAS: f'reftest_element{grid}.html',
            _CanvasType.OFFSCREEN_CANVAS: f'reftest_offscreen{grid}.html',
            _CanvasType.WORKER: f'reftest_worker{grid}.html',
        }
        ref_templates = {
            _TemplateType.REFERENCE: f'reftest_element{grid}.html',
            _TemplateType.HTML_REFERENCE: f'reftest{grid}.html',
            _TemplateType.CAIRO_REFERENCE: f'reftest_img{grid}.html'
        }
        test_output_paths = {
            _CanvasType.HTML_CANVAS: f'{output_files.element}.html',
            _CanvasType.OFFSCREEN_CANVAS: f'{output_files.offscreen}.html',
            _CanvasType.WORKER: f'{output_files.offscreen}.w.html',
        }
        ref_output_paths = {
            _CanvasType.HTML_CANVAS: f'{output_files.element}-expected.html',
            _CanvasType.OFFSCREEN_CANVAS:
                f'{output_files.offscreen}-expected.html',
            _CanvasType.WORKER: (
                f'{output_files.offscreen}.w-expected.html'
                if needs_worker_reference
                else f'{output_files.offscreen}-expected.html'),
        }
        for canvas_type, params in self._canvas_type_params.items():
            params['reference_file'] = pathlib.Path(
                ref_output_paths[canvas_type]).name
            _render(jinja_env, test_templates[canvas_type], params,
                    test_output_paths[canvas_type])

            if canvas_type != _CanvasType.WORKER or needs_worker_reference:
                params['is_test_reference'] = True
                _render(jinja_env, ref_templates[self.template_type], params,
                        ref_output_paths[canvas_type])

    def _write_testharness_test(self, jinja_env: jinja2.Environment,
                                output_files: _OutputPaths):
        grid = '_grid' if len(self.variants) > 1 else ''

        templates = {
            _CanvasType.HTML_CANVAS: f'testharness_element{grid}.html',
            _CanvasType.OFFSCREEN_CANVAS: f'testharness_offscreen{grid}.html',
            _CanvasType.WORKER: f'testharness_worker{grid}.js',
        }
        test_output_files = {
            _CanvasType.HTML_CANVAS: f'{output_files.element}.html',
            _CanvasType.OFFSCREEN_CANVAS: f'{output_files.offscreen}.html',
            _CanvasType.WORKER: f'{output_files.offscreen}.worker.js',
        }

        # Create test cases for canvas, offscreencanvas and worker.
        for canvas_type, params in self._canvas_type_params.items():
            _render(jinja_env, templates[canvas_type], params,
                    test_output_files[canvas_type])

    def _generate_cairo_reference_grid(self,
                                       canvas_type: _CanvasType,
                                       output_dirs: _OutputPaths) -> None:
        """Generate this grid's expected image from Cairo code, if needed.

        In order to cut on the number of files generated, the expected image
        of all the variants in this grid are packed into a single PNG. The
        expected HTML then contains a grid of <img> tags, each showing a portion
        of the PNG file."""
        if not any(v.canvas_type_params[canvas_type].get('cairo_reference')
                   for v in self.variants):
            return

        width, height = self._unique_param([canvas_type], 'size')
        cairo_code = ''

        # First generate a function producing a Cairo surface with the expected
        # image for each variant in the grid. The function is needed to provide
        # a scope isolating the variant code from each other.
        for idx, variant in enumerate(self._variants):
            cairo_ref = variant.canvas_type_params[canvas_type].get(
                'cairo_reference')
            if not cairo_ref:
                raise InvalidTestDefinitionError(
                    'When used, "cairo_reference" must be specified for all '
                    'test variants.')
            cairo_code += textwrap.dedent(f'''\
                def draw_ref{idx}():
                  surface = cairo.ImageSurface(
                      cairo.FORMAT_ARGB32, {width}, {height})
                  cr = cairo.Context(surface)
                {{}}
                  return surface
                  ''').format(textwrap.indent(cairo_ref, '  '))

        # Write all variant images into the final surface.
        surface_width = width * self._grid_width
        surface_height = (height *
                          math.ceil(len(self._variants) / self._grid_width))
        cairo_code += textwrap.dedent(f'''\
            surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32, {surface_width}, {surface_height})
            cr = cairo.Context(surface)
            ''')
        for idx, variant in enumerate(self._variants):
            x_pos = int(idx % self._grid_width) * width
            y_pos = int(idx / self._grid_width) * height
            cairo_code += textwrap.dedent(f'''\
                cr.set_source_surface(draw_ref{idx}(), {x_pos}, {y_pos})
                cr.paint()
                ''')

        img_filename = f'{self.file_name}.png'
        _write_cairo_images(cairo_code, output_dirs.sub_path(img_filename),
                            frozenset([canvas_type]))
        self._canvas_type_params[canvas_type]['img_reference'] = img_filename

    def _generate_cairo_images(self, output_dirs: _OutputPaths) -> None:
        """Generates the pycairo images found in the YAML test definition."""
        # 'expected:' is only used for HTML_CANVAS tests.
        has_expected = any(v.canvas_type_params
                           .get(_CanvasType.HTML_CANVAS, {})
                           .get('expected') for v in self._variants)
        has_cairo_reference = any(
            params.get('cairo_reference')
            for v in self._variants
            for params in v.canvas_type_params.values())

        if has_expected and has_cairo_reference:
            raise InvalidTestDefinitionError(
                'Parameters "expected" and "cairo_reference" can\'t be both '
                'used at the same time.')

        if has_expected:
            if len(self.variants) != 1:
                raise InvalidTestDefinitionError(
                    'Parameter "expected" is not supported for variant grids.')
            if self.template_type != _TemplateType.TESTHARNESS:
                raise InvalidTestDefinitionError(
                    'Parameter "expected" is not supported in reference '
                    'tests.')
            self.variants[0].generate_expected_image(output_dirs)
        elif has_cairo_reference:
            for canvas_type in _CanvasType:
                self._generate_cairo_reference_grid(canvas_type, output_dirs)

    def generate_test(self, jinja_env: jinja2.Environment,
                      output_dirs: _OutputPaths) -> None:
        """Generate the test files to the specified output dirs."""
        self._generate_cairo_images(output_dirs)

        output_files = output_dirs.sub_path(self.file_name)

        if self.template_type in (_TemplateType.REFERENCE,
                                  _TemplateType.HTML_REFERENCE,
                                  _TemplateType.CAIRO_REFERENCE):
            self._write_reference_test(jinja_env, output_files)
        else:
            self._write_testharness_test(jinja_env, output_files)


class _VariantLayout(str, enum.Enum):
    SINGLE_FILE = 'single_file'
    MULTI_FILES = 'multi_files'


@dataclasses.dataclass
class _VariantDimension:
    variants: Mapping[str, _TestParams]
    layout: _VariantLayout


def _get_variant_dimensions(params: _TestParams) -> List[_VariantDimension]:
    variants = params.get('variants', [])
    if not isinstance(variants, list):
        raise InvalidTestDefinitionError(
            textwrap.dedent("""
            Variants must be specified as a list of variant dimensions, e.g.:
                variants:
                - dimension1-variant1:
                    param: ...
                    dimension1-variant2:
                    param: ...
                - dimension2-variant1:
                    param: ...
                    dimension2-variant2:
                    param: ..."""))

    variants_layout = params.get('variants_layout',
                                 [_VariantLayout.MULTI_FILES] * len(variants))
    if len(variants) != len(variants_layout):
        raise InvalidTestDefinitionError(
            'variants and variants_layout must be lists of the same size')
    invalid_layouts = [
        l for l in variants_layout if l not in list(_VariantLayout)
    ]
    if invalid_layouts:
        raise InvalidTestDefinitionError('Invalid variants_layout: ' +
                                         ', '.join(invalid_layouts) +
                                         '. Valid layouts are: ' +
                                         ', '.join(_VariantLayout))

    return [
        _VariantDimension(z[0], z[1]) for z in zip(variants, variants_layout)
    ]


def _get_variant_grids(test: Mapping[str, Any],
                       jinja_env: jinja2.Environment) -> List[_VariantGrid]:
    base_variant = _Variant.create_with_defaults(test)
    grid_width = base_variant.params.get('grid_width', 1)
    if not isinstance(grid_width, int):
        raise InvalidTestDefinitionError('"grid_width" must be an integer.')

    grids = [_VariantGrid([base_variant], grid_width=grid_width)]
    for dimension in _get_variant_dimensions(test):
        variants = dimension.variants
        if dimension.layout == _VariantLayout.MULTI_FILES:
            grids = [
                grid.merge_params(name, params)
                for name, params in variants.items() for grid in grids
            ]
        else:
            grids = [grid.add_dimension(variants) for grid in grids]

    for grid in grids:
        grid.finalize(jinja_env)

    return grids


def _check_uniqueness(tested: DefaultDict[str, Set[_CanvasType]], name: str,
                      canvas_types: FrozenSet[_CanvasType]) -> None:
    already_tested = tested[name].intersection(canvas_types)
    if already_tested:
        raise InvalidTestDefinitionError(
            f'Test {name} is defined twice for types {already_tested}')
    tested[name].update(canvas_types)


def _indent_filter(s: str, width: Union[int, str] = 4,
                   first: bool = False, blank: bool = False) -> str:
    """Returns a copy of the string with each line indented by the `width` str.

    If `width` is a number, `s` is indented by that number of whitespaces. The
    first line and blank lines are not indented by default, unless `first` or
    `blank` are `True`, respectively.

    This is a re-implementation of the default `indent` Jinja filter, preserving
    line ending characters (\r, \n, \f, etc.) The default `indent` Jinja filter
    incorrectly replaces all of these characters with newlines."""
    is_first_line = True
    def indent_needed(line):
        nonlocal first, blank, is_first_line
        is_blank = not line.strip()
        need_indent = (not is_first_line or first) and (not is_blank or blank)
        is_first_line = False
        return need_indent

    indentation = width if isinstance(width, str) else ' ' * width
    return textwrap.indent(s, indentation, indent_needed)


def generate_test_files(name_to_dir_file: str) -> None:
    """Generate Canvas tests from YAML file definition."""
    output_dirs = _OutputPaths(element=pathlib.Path('..') / 'element',
                               offscreen=pathlib.Path('..') / 'offscreen')

    jinja_env = jinja2.Environment(
        loader=jinja2.PackageLoader('gentestutilsunion'),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True)

    jinja_env.filters['double_quote_escape'] = _double_quote_escape
    jinja_env.filters['indent'] = _indent_filter

    # Run with --test argument to run unit tests.
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        doctest = importlib.import_module('doctest')
        doctest.testmod()
        sys.exit()

    name_to_sub_dir = (yaml.safe_load(
        pathlib.Path(name_to_dir_file).read_text(encoding='utf-8')))

    tests = []
    test_yaml_directory = 'yaml-new'
    yaml_files = [
        os.path.join(test_yaml_directory, f)
        for f in os.listdir(test_yaml_directory) if f.endswith('.yaml')
    ]
    for t in sum([
            yaml.safe_load(pathlib.Path(f).read_text(encoding='utf-8'))
            for f in yaml_files
    ], []):
        if 'DISABLED' in t:
            continue
        if 'meta' in t:
            eval(compile(t['meta'], '<meta test>', 'exec'), {},
                 {'tests': tests})
        else:
            tests.append(t)

    for sub_dir in set(name_to_sub_dir.values()):
        output_dirs.sub_path(sub_dir).mkdir()

    used_filenames = collections.defaultdict(set)
    used_variants = collections.defaultdict(set)
    for test in tests:
        print(test['name'])
        for grid in _get_variant_grids(test, jinja_env):
            if test['name'] != grid.file_name:
                print(f'  {grid.file_name}')

            _check_uniqueness(used_filenames, grid.file_name,
                              grid.canvas_types)
            for variant in grid.variants:
                _check_uniqueness(
                    used_variants,
                    '.'.join([grid.file_name] +
                             variant.params['grid_variant_names']),
                    grid.canvas_types)

            sub_dir = _get_test_sub_dir(grid.file_name, name_to_sub_dir)
            grid.generate_test(jinja_env, output_dirs.sub_path(sub_dir))

    print()
