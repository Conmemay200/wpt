#!/usr/bin/env python3
import argparse
import os

from . import manifest
from . import vcs
from .log import get_logger, enable_debug_logging
from .download import download_from_github

here = os.path.dirname(__file__)

wpt_root = os.path.abspath(os.path.join(here, os.pardir, os.pardir))

logger = get_logger()

MYPY = False
if MYPY:
    # MYPY is set to True when run under Mypy.
    from typing import Any
    from typing import Optional
    from .manifest import Manifest  # avoid cyclic import


def update(tests_root,  # type: str
           manifest,  # type: Manifest
           manifest_path=None,  # type: Optional[str]
           working_copy=True,  # type: bool
           cache_root=None,  # type: Optional[str]
           rebuild=False,  # type: bool
           parallel=True,  # type: bool
           sub_dirs=None  # type: Optional[list[str]]
           ):
    # type: (...) -> bool
    logger.warning("Deprecated; use manifest.load_and_update instead")
    logger.info("Updating manifest")

    tree = vcs.get_tree(tests_root, manifest, manifest_path, cache_root,
                        working_copy, rebuild, sub_dirs)
    return manifest.update(tree, parallel)


def update_from_cli(**kwargs):
    # type: (**Any) -> None
    tests_root = kwargs["tests_root"]
    path = kwargs["path"]
    assert tests_root is not None

    if not kwargs["rebuild"] and kwargs["download"]:
        download_from_github(path, tests_root)

    manifest.load_and_update(tests_root,
                             path,
                             kwargs["url_base"],
                             update=True,
                             rebuild=kwargs["rebuild"],
                             cache_root=kwargs["cache_root"],
                             parallel=kwargs["parallel"],
                             sub_dirs=kwargs["dirs"])


def abs_path(path):
    # type: (str) -> str
    return os.path.abspath(os.path.expanduser(path))


def create_parser():
    # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v", "--verbose", dest="verbose", action="store_true", default=False,
        help="Turn on verbose logging")
    parser.add_argument(
        "-p", "--path", type=abs_path, help="Path to manifest file.")
    parser.add_argument(
        "--tests-root", type=abs_path, default=wpt_root, help="Path to root of tests.")
    parser.add_argument(
        "-r", "--rebuild", action="store_true", default=False,
        help="Force a full rebuild of the manifest.")
    parser.add_argument(
        "--url-base", action="store", default="/",
        help="Base url to use as the mount point for tests in this manifest.")
    parser.add_argument(
        "--no-download", dest="download", action="store_false", default=True,
        help="Never attempt to download the manifest.")
    parser.add_argument(
        "--cache-root", action="store", default=os.path.join(wpt_root, ".wptcache"),
        help="Path in which to store any caches (default <tests_root>/.wptcache/)")
    parser.add_argument(
        "--no-parallel", dest="parallel", action="store_false", default=True,
        help="Do not parallelize building the manifest")
    parser.add_argument('dirs', nargs='*', default=[],
                        help='sub directories or files relative to the tests root to update.')
    return parser


def run(*args, **kwargs):
    # type: (*Any, **Any) -> None
    if kwargs["path"] is None:
        kwargs["path"] = os.path.join(kwargs["tests_root"], "MANIFEST.json")
    if kwargs["verbose"]:
        enable_debug_logging()
    update_from_cli(**kwargs)


def main():
    # type: () -> None
    opts = create_parser().parse_args()

    run(**vars(opts))
