#!/usr/bin/env python3

"""
Generate various docker and config files across a matrix of archs/distros.
"""

import abc
import argparse
import contextlib
import functools
import itertools
import json
import os
import pathlib
import re
import shutil
import sys
import time

import semver

from github import Github
from jinja2 import (
    contextfilter,
    Environment,
    StrictUndefined,
)
from path import Path
from ruamel import yaml
from sh import docker as _docker  # pylint: disable=no-name-in-module
from sh import docker_compose as _docker_compose  # pylint: disable=no-name-in-module
from sh import ErrorReturnCode_1  # pylint: disable=no-name-in-module
from sh import which  # pylint: disable=no-name-in-module


docker = functools.partial(_docker, _out=sys.stdout, _err=sys.stderr)
docker_compose = functools.partial(_docker_compose, _out=sys.stdout, _err=sys.stderr)

PROJECT_DIR = Path(os.path.abspath(os.path.dirname(__file__)))
MIN_NIM_VERSION = semver.VersionInfo.parse("0.20.2")


@functools.lru_cache(maxsize=None)
def get_nim_versions():
    gh = Github(os.environ["GITHUB_TOKEN"])
    repo = gh.get_repo("nim-lang/Nim")
    versions = {}
    for tag in repo.get_tags():
        if tag.name.startswith("v"):
            try:
                version = semver.VersionInfo.parse(tag.name[1:])
            except ValueError:
                continue
            else:
                if version >= MIN_NIM_VERSION:
                    key = (version.major, version.minor)
                    try:
                        if versions[key][0] < version:
                            versions[key] = (version, tag)
                    except KeyError:
                        versions[key] = (version, tag)
    return sorted([v[1].name for v in versions.values()])


print(get_nim_versions())


def get_image_slug(*args):
    return "-".join(map(slugify, args))


class Dumper(yaml.RoundTripDumper):
    def ignore_aliases(self, data):
        # Strip aliases
        return True


def slugify(string):
    return re.sub(r"[^\w]", "-", string).lower()


docker_manifest_args = {
    "amd64": ["--arch", "amd64"],
    "i386": ["--arch", "386"],
    "arm32v6": ["--arch", "arm", "--variant", "v6"],
    "arm32v7": ["--arch", "arm", "--variant", "v7"],
    "arm64v8": ["--arch", "arm64", "--variant", "v8"],
    "ppc64le": ["--arch", "ppc64le"],
    "s390x": ["--arch", "s390x"],
}


class Distro(metaclass=abc.ABCMeta):
    template_path = None
    registry = {}
    archs = ()

    def __init__(self, name):
        self.name = name
        self.registry[name] = self
        self._context = None
        self.env = Environment(autoescape=False, undefined=StrictUndefined)
        self.env.filters["slugify"] = slugify
        self.env.filters["image_slug"] = get_image_slug

    def __repr__(self):
        return f"{self.__class__.__name__}({repr(self.name)})"

    @classmethod
    def get(cls, name):
        if name not in cls.registry:
            raise ValueError(
                f'Unsupported distro {name}, choose from {", ".join(cls.registry.keys())}'
            )
        return cls.registry[name]

    @classmethod
    def clean_all(cls):
        for distro in cls.registry.values():
            distro.clean()

    @classmethod
    def render_all(cls, **context):
        for distro in cls.registry.values():
            distro.render(**context)

    @classmethod
    def build_all(cls, push=False):
        for distro in cls.registry.values():
            for arch in distro.archs:
                for nim_version in get_nim_versions():
                    distro.build(arch, nim_version=nim_version, push=push)

    @property
    def context(self):
        if self._context is None:
            raise RuntimeError("Distro context not entered")
        return self._context

    @contextlib.contextmanager
    def set_context(self, **context):
        prev_context = self._context.copy() if self._context else None
        new_context = self._context.copy() if self._context else {}
        new_context.update(context)
        self._context = self.get_template_context(**new_context)
        try:
            yield
        finally:
            self._context = prev_context

    def render_template(self, template_path, out_path):
        with PROJECT_DIR:
            template_path = template_path.format(**self.context)
            with open(template_path, "r") as f:
                rendered = self.env.from_string(f.read()).render(**self.context)
            out_path = out_path.format(**self.context)
            dir_name = os.path.dirname(out_path)
            if not os.path.exists(dir_name):
                os.makedirs(dir_name)
            rendered = f"# Rendered from {template_path}\n\n" + rendered
            with open(out_path, "w") as f:
                f.write(rendered)
            print(f"Rendered {template_path} -> {out_path}")
            return out_path

    def interpolate_yaml(self, yaml_path):
        with PROJECT_DIR:
            yaml_path = yaml_path.format(**self.context)
            with open(yaml_path, "r") as f:
                rendered = yaml.load(f, Loader=yaml.RoundTripLoader)

            # Remove _anchors section
            if "_anchors" in rendered:
                del rendered["_anchors"]

            # Use custom Dumper that replaces aliases with referenced content
            rendered = yaml.dump(rendered, Dumper=Dumper)

            with open(yaml_path, "w") as f:
                f.write(rendered)

            print(f"Interpolated {yaml_path}")
            return yaml_path

    @property
    def docker_compose_yml_path(self):
        return self.out_path / f"docker-compose.yml"

    @property
    def github_actions_yml_path(self):
        return Path(f".github/workflows/{slugify(self.name)}.yml")

    def clean(self):
        if os.path.exists(self.out_path):
            shutil.rmtree(self.out_path)
            print(f"Removed {self.out_path}")

    @property
    def out_path(self):
        return Path(slugify(self.name))

    def get_template_context(self, **context):
        nim_versions = get_nim_versions()
        context.update(
            dict(
                distro=self.name,
                distro_slug=slugify(self.name),
                archs=self.archs,
                nim_versions=nim_versions,
                nim_versions_csv=",".join(nim_versions),
            )
        )
        return context

    def render(self, **context):
        with self.set_context(**context):
            self.render_dockerfiles()
            self.render_docker_compose()
            self.render_github_actions()

            with PROJECT_DIR:
                for root, _, files in os.walk(self.template_path):
                    root = Path(root)
                    new_root = Path(root.replace(self.template_path, self.out_path))
                    for f in files:
                        if ".jinja" in f:
                            continue
                        if not os.path.exists(os.path.dirname(new_root / f)):
                            os.makedirs(os.path.dirname(new_root / f))
                        shutil.copyfile(root / f, new_root / f)
                        print(f"Copied {root / f} -> {new_root / f}")

                for root, _, files in os.walk(PROJECT_DIR / "scripts"):
                    root = Path(root)
                    new_root = Path(root.replace("scripts", self.out_path / "scripts"))
                    for f in files:
                        if ".jinja" in f:
                            continue
                        if not os.path.exists(os.path.dirname(new_root / f)):
                            os.makedirs(os.path.dirname(new_root / f))
                        shutil.copyfile(root / f, new_root / f)
                        print(f"Copied {root / f} -> {new_root / f}")

    def render_dockerfiles(self):
        for arch in self.archs:
            for nim_version in get_nim_versions():
                with self.set_context(arch=arch, nim_version=nim_version):
                    self.render_template(
                        self.template_path / "Dockerfile.jinja",
                        self.out_path
                        / f"{get_image_slug(self.name, nim_version, arch)}.dockerfile",
                    )

    def render_docker_compose(self):
        self.render_template(
            self.template_path / "docker-compose.yml.jinja",
            self.docker_compose_yml_path,
        )

    def render_github_actions(self):
        with self.set_context():
            self.render_template(
                Path(".github/workflows/build.yml.jinja"),
                self.github_actions_yml_path,
            )
            # Replace YAML aliases in rendered jinja output
            self.interpolate_yaml(self.github_actions_yml_path)

    def render_github_actions(self):
        with self.set_context():
            self.render_template(
                Path(".github/workflows/build.yml.jinja"),
                self.github_actions_yml_path,
            )
            # Replace YAML aliases in rendered jinja output
            self.interpolate_yaml(self.github_actions_yml_path)

    def build(self, arch, nim_version, push=False):
        self.render()
        image_slug = get_image_slug(self.name, nim_version, arch)
        image = f"elijahru/nim:{image_slug}"
        dockerfile = self.out_path / f"{image_slug}.dockerfile"
        try:
            docker("pull", image)
        except ErrorReturnCode_1:
            pass
        with self.run_build_host():
            docker(
                "build",
                self.out_path,
                "--file",
                dockerfile,
                "--tag",
                image,
                "--cache-from",
                image,
                "--build-arg",
                f"NIM_VERSION={nim_version}",
            )
        if push:
            docker("push", image)

    def push_manifest(self, nim_version):
        os.environ["DOCKER_CLI_EXPERIMENTAL"] = "enabled"
        manifest = f"elijahru/nim:{get_image_slug(self.name, nim_version)}"
        images = {
            arch: f"elijahru/nim:{get_image_slug(self.name, nim_version, arch)}"
            for arch in self.archs
        }

        for image in images.values():
            try:
                docker("pull", image)
            except ErrorReturnCode_1:
                pass

        try:
            docker("manifest", "create", "--amend", manifest, *images.values())
        except ErrorReturnCode_1:
            docker("manifest", "create", manifest, *images.values())

        for arch in self.archs:
            docker(
                "manifest",
                "annotate",
                manifest,
                images[arch],
                "--os",
                "linux",
                *docker_manifest_args[arch],
            )

        docker("manifest", "push", manifest)

    @contextlib.contextmanager
    def run_build_host(self):
        image_id = lambda: docker(
            "ps",
            "--filter",
            "name=build-host",
            "--format",
            "{{.ID}}",
            _out=None,
            _err=None,
        ).strip()
        id = image_id()
        if id:
            docker("kill", id, _out=None, _err=None)
        docker_compose("-f", self.docker_compose_yml_path, "up", "-d", "build-host")
        time.sleep(5)
        try:
            yield
        finally:
            id = image_id()
            if id:
                docker("kill", id, _out=None, _err=None)

    def test(self, arch, nim_version):
        self.render()

        with self.run_build_host():
            image_slug = get_image_slug(self.name, nim_version, arch)
            docker_compose("-f", self.docker_compose_yml_path, "run", image_slug)


class DebianLike(Distro):
    template_path = Path("debian-like")

    archs = ("amd64", "i386", "arm32v7", "arm64v8", "ppc64le", "s390x")


class ArchLinuxLike(Distro):
    template_path = Path("archlinux-like")

    archs = (
        "amd64",
        "arm32v6",
        "arm32v7",
        "arm64v8",
    )


# Register supported distributions
debian_buster = DebianLike("debian:buster")
archlinux = ArchLinuxLike("archlinux")


def make_parser():
    parser = argparse.ArgumentParser()

    subparsers = parser.add_subparsers(dest="subcommand")

    # list-distros
    subparsers.add_parser("list-distros")

    # list-archs
    parser_list_archs = subparsers.add_parser("list-archs")
    parser_list_archs.add_argument("--distro", type=Distro.get, required=True)

    # list-nim-versions
    parser_list_nim_versions = subparsers.add_parser("list-nim-versions")

    # render
    parser_render = subparsers.add_parser("render")

    # render
    subparsers.add_parser("render-github-actions")

    # build
    parser_build = subparsers.add_parser("build")
    parser_build.add_argument("--distro", type=Distro.get, required=True)
    parser_build.add_argument("--arch", required=True)
    parser_build.add_argument("--nim-version", required=True)
    parser_build.add_argument("--push", action="store_true")

    # build-all
    parser_build_all = subparsers.add_parser("build-all")
    parser_build_all.add_argument("--push", action="store_true")

    # clean
    subparsers.add_parser("clean")

    # test
    parser_test = subparsers.add_parser("test")
    parser_test.add_argument("--distro", type=Distro.get, required=True)
    parser_test.add_argument("--arch", required=True)
    parser_test.add_argument("--nim-version", required=True)

    # push-manifest
    parser_push_manifest = subparsers.add_parser("push-manifest")
    parser_push_manifest.add_argument("--distro", type=Distro.get, required=True)
    parser_push_manifest.add_argument("--nim-version", required=True)

    return parser


def main():
    args = make_parser().parse_args()
    if args.subcommand == "list-distros":
        print("\n".join(Distro.registry.keys()))

    elif args.subcommand == "list-archs":
        print("\n".join(args.distro.archs))

    elif args.subcommand == "list-nim-versions":
        print("\n".join(get_nim_versions()))

    elif args.subcommand == "render":
        Distro.render_all()

    elif args.subcommand == "render-github-actions":
        for distro in Distro.registry.values():
            distro.render_github_actions()

    elif args.subcommand == "build":
        args.distro.build(args.arch, nim_version=args.nim_version, push=args.push)

    elif args.subcommand == "build-all":
        Distro.build_all(push=args.push)

    elif args.subcommand == "clean":
        Distro.clean_all()

    elif args.subcommand == "test":
        args.distro.test(args.arch, nim_version=args.nim_version)

    elif args.subcommand == "push-manifest":
        args.distro.push_manifest(nim_version=args.nim_version)

    else:
        raise ValueError(f"Unknown subcommand {args.subcommand}")


if __name__ == "__main__":
    main()
