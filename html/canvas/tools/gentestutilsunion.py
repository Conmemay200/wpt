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

from typing import Any, DefaultDict, FrozenSet, List, Mapping, MutableMapping
from typing import Optional, Set, Tuple

import re
import collections
import dataclasses
import enum
import importlib
import itertools
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


def _unroll(text: str) -> str:
    """Unrolls text with all possible permutations of the parameter lists.

    Example:
    >>> print _unroll('f = {<a | b>: <1 | 2 | 3>};')
    // a
    f = {a: 1};
    f = {a: 2};
    f = {a: 3};
    // b
    f = {b: 1};
    f = {b: 2};
    f = {b: 3};
    """
    patterns = []  # type: List[Tuple[str, List[str]]]
    while True:
        match = re.search(r'<([^>]+)>', text)
        if not match:
            break
        key = f'@unroll_pattern_{len(patterns)}'
        values = text[match.start(1):match.end(1)]
        text = text[:match.start(0)] + key + text[match.end(0):]
        patterns.append((key, [value.strip() for value in values.split('|')]))

    def unroll_patterns(text: str,
                        patterns: List[Tuple[str, List[str]]],
                        label: Optional[str] = None) -> List[str]:
        if not patterns:
            return [text]
        patterns = patterns.copy()
        key, values = patterns.pop(0)
        return (['// ' + label] if label else []) + list(
            itertools.chain.from_iterable(
                unroll_patterns(text.replace(key, value), patterns, value)
                for value in values))

    result = '\n'.join(unroll_patterns(text, patterns))
    return result


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

    code = _remove_extra_newlines(code)

    # Unroll expressions with a cross-product-style parameter expansion.
    code = re.sub(r'@unroll ([^;]*;)', lambda m: _unroll(m.group(1)), code)

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
    TESTHARNESS = 'testharness'


@dataclasses.dataclass
class _OutputPaths:
    element: str
    offscreen: str

    def sub_path(self, sub_dir: str):
        """Create a new _OutputPaths that is a subpath of this _OutputPath."""
        return _OutputPaths(element=os.path.join(self.element, sub_dir),
                            offscreen=os.path.join(self.offscreen, sub_dir))


def _validate_test(test: _TestParams):
    if test.get('expected', '') == 'green' and re.search(
            r'@assert pixel .* 0,0,0,0;', test['code']):
        print(f'Probable incorrect pixel test in {test["name"]}')

    if 'size' in test and (not isinstance(test['size'], list)
                           or len(test['size']) != 2):
        raise InvalidTestDefinitionError(
            f'Invalid canvas size "{test["size"]}" in test {test["name"]}. '
            'Expected an array with two numbers.')

    if 'test_type' in test and test['test_type'] != 'promise':
        raise InvalidTestDefinitionError(
            f'Test {test["name"]}\' test_type is invalid, it only accepts '
            '"promise" now for creating promise test type in the template '
            'file.')


def _render_template(jinja_env: jinja2.Environment, template: jinja2.Template,
                     params: _TestParams) -> str:
    """Renders the specified jinja template.

    The template is repetitively rendered until no more changes are observed.
    This allows for template parameters to refer to other template parameters.
    """
    rendered = template.render(params)
    previous = ''
    while rendered != previous:
        previous = rendered
        template = jinja_env.from_string(rendered)
        rendered = template.render(params)
    return rendered


def _render(jinja_env: jinja2.Environment, template_name: str,
            params: _TestParams):
    params = dict(params)
    params.update({
        # Render the code on its own, as it could contain templates expanding
        # to multiple lines. This is needed to get proper indentation of the
        # code in the main template.
        'code': _render_template(jinja_env,
                                 jinja_env.from_string(params['code']),
                                 params)
    })

    return _render_template(jinja_env, jinja_env.get_template(template_name),
                            params)


def _add_default_params(test: _TestParams) -> _TestParams:
    params = {
        'desc': '',
        'size': [100, 50],
        'variant_names': [],
    }
    params.update(test)
    return params


def _get_variant_name(jinja_env: jinja2.Environment,
                      params: _TestParams) -> str:
    name = params['name']
    if params.get('append_variants_to_name', True):
        name = '.'.join([name] + params['variant_names'])

    name = jinja_env.from_string(name).render(params)
    return name


def _get_file_name(params: _TestParams) -> str:
    file_name = params['name']
    if 'manual' in params:
        file_name += '-manual'
    return file_name


def _get_canvas_types(params: _TestParams) -> FrozenSet[_CanvasType]:
    canvas_types = params.get('canvas_types', _CanvasType)
    invalid_types = {
        type
        for type in canvas_types if type not in list(_CanvasType)
    }
    if invalid_types:
        raise InvalidTestDefinitionError(
            f'Invalid canvas_types: {list(invalid_types)}. '
            f'Accepted values are: {[t.value for t in _CanvasType]}')
    return frozenset(_CanvasType(t) for t in canvas_types)


def _get_template_type(params: _TestParams) -> _TemplateType:
    if 'reference' in params and 'html_reference' in params:
        raise InvalidTestDefinitionError(
            f'Test {params["name"]} is invalid, "reference" and '
            '"html_reference" can\'t both be specified at the same time.')

    if 'reference' in params:
        return _TemplateType.REFERENCE
    if 'html_reference' in params:
        return _TemplateType.HTML_REFERENCE
    return _TemplateType.TESTHARNESS


def _finalize_params(jinja_env: jinja2.Environment,
                     params: _MutableTestParams) -> None:
    params['name'] = _get_variant_name(jinja_env, params)
    params['file_name'] = _get_file_name(params)
    params['canvas_types'] = _get_canvas_types(params)
    params['template_type'] = _get_template_type(params)
    params['code'] = _expand_test_code(params['code'])


def _write_reference_test(jinja_env: jinja2.Environment, params: _TestParams,
                          output_files: _OutputPaths):
    if _CanvasType.HTML_CANVAS in params['canvas_types']:
        html_params = dict(params)
        html_params.update({'canvas_type': _CanvasType.HTML_CANVAS.value})
        pathlib.Path(f'{output_files.element}.html').write_text(
            _render(jinja_env, 'reftest_element.html', html_params), 'utf-8')
    if _CanvasType.OFFSCREEN_CANVAS in params['canvas_types']:
        offscreen_params = dict(params)
        offscreen_params.update(
            {'canvas_type': _CanvasType.OFFSCREEN_CANVAS.value})
        pathlib.Path(f'{output_files.offscreen}.html').write_text(
            _render(jinja_env, 'reftest_offscreen.html', offscreen_params),
            'utf-8')
    if _CanvasType.WORKER in params['canvas_types']:
        worker_params = dict(params)
        worker_params.update({'canvas_type': _CanvasType.WORKER.value})
        pathlib.Path(f'{output_files.offscreen}.w.html').write_text(
            _render(jinja_env, 'reftest_worker.html', worker_params), 'utf-8')

    js_ref = params.get('reference', '')
    html_ref = params.get('html_reference', '')
    ref_params = dict(params)
    ref_params.update({
        'is_test_reference': True,
        'code': js_ref or html_ref
    })
    ref_template_name = 'reftest_element.html' if js_ref else 'reftest.html'
    if _CanvasType.HTML_CANVAS in params['canvas_types']:
        pathlib.Path(f'{output_files.element}-expected.html').write_text(
            _render(jinja_env, ref_template_name, ref_params), 'utf-8')
    if {_CanvasType.OFFSCREEN_CANVAS, _CanvasType.WORKER
        } & params['canvas_types']:
        pathlib.Path(f'{output_files.offscreen}-expected.html').write_text(
            _render(jinja_env, ref_template_name, ref_params), 'utf-8')


def _write_testharness_test(jinja_env: jinja2.Environment, params: _TestParams,
                            output_files: _OutputPaths):
    # Create test cases for canvas and offscreencanvas.
    if _CanvasType.HTML_CANVAS in params['canvas_types']:
        html_params = dict(params)
        html_params.update({'canvas_type': _CanvasType.HTML_CANVAS.value})
        pathlib.Path(f'{output_files.element}.html').write_text(
            _render(jinja_env, 'testharness_element.html', html_params),
            'utf-8')

    if _CanvasType.OFFSCREEN_CANVAS in params['canvas_types']:
        offscreen_params = dict(params)
        offscreen_params.update(
            {'canvas_type': _CanvasType.OFFSCREEN_CANVAS.value})
        pathlib.Path(f'{output_files.offscreen}.html').write_text(
            _render(jinja_env, 'testharness_offscreen.html', offscreen_params),
            'utf-8')

    if _CanvasType.WORKER in params['canvas_types']:
        worker_params = dict(params)
        worker_params.update({'canvas_type': _CanvasType.WORKER.value})
        pathlib.Path(f'{output_files.offscreen}.worker.js').write_text(
            _render(jinja_env, 'testharness_worker.js', worker_params),
            'utf-8')


def _generate_expected_image(params: _MutableTestParams,
                             output_dirs: _OutputPaths) -> None:
    """Creates a reference image using Cairo and save filename in params."""
    if 'expected' not in params:
        return

    expected = params['expected']
    name = params['name']

    if expected == 'green':
        params['expected_img'] = '/images/green-100x50.png'
        return
    if expected == 'clear':
        params['expected_img'] = '/images/clear-100x50.png'
        return
    if ';' in expected:
        print(f'Found semicolon in {name}')
    expected = re.sub(
        r'^size (\d+) (\d+)',
        r'surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, \1, \2)'
        r'\ncr = cairo.Context(surface)', expected)

    output_paths = output_dirs.sub_path(name)
    if _CanvasType.HTML_CANVAS in params['canvas_types']:
        expected_canvas = (
            f'{expected}\n'
            f'surface.write_to_png("{output_paths.element}.png")\n')
        eval(compile(expected_canvas, f'<test {name}>', 'exec'), {},
             {'cairo': cairo})

    if {_CanvasType.OFFSCREEN_CANVAS, _CanvasType.WORKER
        } & params['canvas_types']:
        expected_offscreen = (
            f'{expected}\n'
            f'surface.write_to_png("{output_paths.offscreen}.png")\n')
        eval(compile(expected_offscreen, f'<test {name}>', 'exec'), {},
             {'cairo': cairo})

    params['expected_img'] = f'{name}.png'


def _generate_test(params: _TestParams, jinja_env: jinja2.Environment,
                   output_dirs: _OutputPaths) -> None:
    output_files = output_dirs.sub_path(params['file_name'])
    if params['template_type'] in (_TemplateType.REFERENCE,
                                   _TemplateType.HTML_REFERENCE):
        _write_reference_test(jinja_env, params, output_files)
    else:
        _write_testharness_test(jinja_env, params, output_files)


def _recursive_expand_variant_matrix(original_test: _TestParams,
                                     variant_matrix: List[_TestParams],
                                     current_selection: List[Tuple[str, Any]],
                                     test_variants: List[_MutableTestParams]):
    if len(current_selection) == len(variant_matrix):
        # Selection for each variant is done, so add a new test to test_list.
        test = dict(original_test)
        variant_name_list = []
        for variant_name, variant_params in current_selection:
            test.update(variant_params)
            variant_name_list.append(variant_name)
        # Expose variant names as a list so they can be used from the yaml
        # files, which helps with better naming of tests.
        test.update({'variant_names': variant_name_list})
        test_variants.append(test)
    else:
        # Continue the recursion with each possible selection for the current
        # variant.
        variant = variant_matrix[len(current_selection)]
        for variant_options in variant.items():
            current_selection.append(variant_options)
            _recursive_expand_variant_matrix(original_test, variant_matrix,
                                             current_selection, test_variants)
            current_selection.pop()


def _get_variants(test: _TestParams) -> List[_MutableTestParams]:
    current_selection = []
    test_variants = []
    variants = test.get('variants', [])
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
    _recursive_expand_variant_matrix(test, variants, current_selection,
                                     test_variants)
    return test_variants


def _check_uniqueness(tested: DefaultDict[str, Set[_CanvasType]], name: str,
                      canvas_types: FrozenSet[_CanvasType]) -> None:
    already_tested = tested[name].intersection(canvas_types)
    if already_tested:
        raise InvalidTestDefinitionError(
            f'Test {name} is defined twice for types {already_tested}')
    tested[name].update(canvas_types)


def generate_test_files(name_to_dir_file: str) -> None:
    """Generate Canvas tests from YAML file definition."""
    output_dirs = _OutputPaths(element='../element', offscreen='../offscreen')

    jinja_env = jinja2.Environment(
        loader=jinja2.PackageLoader('gentestutilsunion'),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True)

    jinja_env.filters['double_quote_escape'] = _double_quote_escape

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

    # Ensure the test output directories exist.
    test_dirs = [output_dirs.element, output_dirs.offscreen]
    for sub_dir in set(name_to_sub_dir.values()):
        test_dirs.append(f'{output_dirs.element}/{sub_dir}')
        test_dirs.append(f'{output_dirs.offscreen}/{sub_dir}')
    for d in test_dirs:
        try:
            os.mkdir(d)
        except FileExistsError:
            pass  # Ignore if it already exists,

    used_tests = collections.defaultdict(set)
    for test in tests:
        print(test['name'])
        _validate_test(test)
        test = _add_default_params(test)
        for variant in _get_variants(test):
            _finalize_params(jinja_env, variant)
            if test['name'] != variant['name']:
                print(f'  {variant["name"]}')

            sub_dir = _get_test_sub_dir(variant['file_name'], name_to_sub_dir)
            output_sub_dirs = output_dirs.sub_path(sub_dir)

            _check_uniqueness(used_tests, variant['name'],
                              variant['canvas_types'])
            _generate_expected_image(variant, output_sub_dirs)
            _generate_test(variant, jinja_env, output_sub_dirs)

    print()
