import logging
from functools import cached_property
from types import GenericAlias, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

import configargparse


class Option:
    DOC_SUFFIX = "__DOC"
    DYNAMIC_DEFAULT_SUFFIX = "__DYNAMIC_DEFAULT"
    SHORT_SUFFIX = "__SHORT"

    def __init__(self, parent: "ConfigModule", name: str) -> None:
        self.parent = parent
        self.config_obj = parent.config_obj
        self.name = name

    @cached_property
    def doc(self) -> str:
        return getattr(self.config_obj, self.name + self.DOC_SUFFIX)  # type:ignore

    @cached_property
    def required(self) -> bool:
        return not hasattr(
            self.config_obj, self.name + self.DYNAMIC_DEFAULT_SUFFIX
        ) and not hasattr(self.config_obj, self.name)

    @cached_property
    def default(self) -> object:
        return getattr(self.config_obj, self.name, None)

    @cached_property
    def short(self) -> str | None:
        return getattr(self.config_obj, self.name + self.SHORT_SUFFIX, None)

    @cached_property
    def nargs(self) -> str | int | None:
        type_ = get_type_hints(self.config_obj).get(self.name, type(self.default))

        if isinstance(type_, GenericAlias):
            args = get_args(type_)
            if args[1] is Ellipsis:
                return "*"
            else:
                return len(args)
        return None

    @cached_property
    def type(self) -> object:
        type_ = get_type_hints(self.config_obj).get(self.name, type(self.default))

        if _is_optional(type_):
            type_ = get_args(type_)[0]
        elif isinstance(type_, GenericAlias):
            args = get_args(type_)
            type_ = args[0]

        return type_

    @cached_property
    def names(self) -> list[str]:
        res = ["--" + self.name.lower().replace("_", "-")]
        if s := self.short:
            res.append("-" + s)
        return res

    @cached_property
    def kwargs(self) -> dict[str, Any]:
        kwargs = dict(
            required=self.required,
            help=self.doc,
            env_var=self.name_to_env_var(),
        )
        t = self.type
        if t is bool:
            if self.default:
                kwargs["action"] = "store_false"
            else:
                kwargs["action"] = "store_true"
        else:
            kwargs["type"] = t
            if self.required:
                kwargs["required"] = True
            else:
                kwargs["default"] = self.default
        if n := self.nargs:
            kwargs["nargs"] = n
        return kwargs

    def name_to_env_var(self) -> str:
        return self.parent.ENV_VAR_PREFIX + self.name


class ConfigModule:
    ENV_VAR_PREFIX = "SLIDGE_"

    def __init__(
        self,
        config_obj: object,
        parser: configargparse.ArgumentParser | None = None,
        skip_options: tuple[str, ...] = (),
    ) -> None:
        self.config_obj = config_obj
        if parser is None:
            parser = configargparse.ArgumentParser()
        self.parser = parser

        self.skip_options = skip_options
        self.add_options_to_parser(skip_options)

    def _list_options(self) -> set[str]:
        return {
            o
            for o in (set(dir(self.config_obj)) | set(get_type_hints(self.config_obj)))
            if o.upper() == o
            and not o.startswith("_")
            and "__" not in o
            and o.lower() not in self.skip_options
        }

    def set_conf(
        self, argv: list[str] | None = None
    ) -> tuple[configargparse.Namespace, list[str]]:
        if argv is not None:
            # this is ugly, but necessary because for plugin config, we used
            # remaining argv.
            # when using (a) .ini file(s), for bool options, we end-up with
            # remaining pseudo-argv such as --some-bool-opt=true when we really
            # should have just --some-bool-opt
            # TODO: get rid of configargparse and make this cleaner
            options_long = {o.name: o for o in self.options}
            no_explicit_bool = []
            skip_next = False
            for a, aa in zip(argv, [*argv[1:], ""]):
                if skip_next:
                    skip_next = False
                    continue
                force_keep = False
                if "=" in a:
                    real_name, _value = a.split("=")
                    opt: Option | None = options_long.get(
                        _argv_to_option_name(real_name)
                    )
                    if opt and opt.type is bool:
                        if opt.default:
                            if _value in _TRUEISH or not _value:
                                continue
                            else:
                                a = real_name
                                force_keep = True
                        else:
                            if _value in _TRUEISH:
                                a = real_name
                                force_keep = True
                            else:
                                continue
                else:
                    upper = _argv_to_option_name(a)
                    opt = options_long.get(upper)
                    if (
                        opt
                        and opt.type is bool
                        and (
                            not aa.startswith("-")
                            and _argv_to_option_name(aa) not in options_long
                        )
                    ):
                        log.debug("Removing %s from argv", aa)
                        skip_next = True

                if opt:
                    if opt.type is bool:
                        if force_keep or not opt.default:
                            no_explicit_bool.append(a)
                    else:
                        no_explicit_bool.append(a)
                else:
                    no_explicit_bool.append(a)
            log.debug("Removed boolean values from %s to %s", argv, no_explicit_bool)
            argv = no_explicit_bool

        args, rest = self.parser.parse_known_args(argv)
        self.update_dynamic_defaults(args)
        for name in self._list_options():
            value = getattr(args, name.lower())
            log.debug("Setting '%s' to %r", name, value)
            setattr(self.config_obj, name, value)
        return args, rest

    @cached_property
    def options(self) -> list[Option]:
        res = []
        for opt in self._list_options():
            res.append(Option(self, opt))
        return res

    def add_options_to_parser(self, skip_options: tuple[str, ...]) -> None:
        skip_options = tuple(o.lower() for o in skip_options)
        p = self.parser
        for o in sorted(self.options, key=lambda x: (not x.required, x.name)):
            if o.name.lower() in skip_options:
                continue
            p.add_argument(*o.names, **o.kwargs)

    def update_dynamic_defaults(self, args: configargparse.Namespace) -> None:
        pass


def _is_optional(t: object) -> bool:
    origin = get_origin(t)
    if origin is Union or origin is UnionType:
        args = get_args(t)
        if len(args) == 2 and type(None) in args:
            return True
    return False


def _argv_to_option_name(arg: str) -> str:
    return arg.upper().removeprefix("--").replace("-", "_")


_TRUEISH = {"true", "True", "1", "on", "enabled"}


log = logging.getLogger(__name__)
