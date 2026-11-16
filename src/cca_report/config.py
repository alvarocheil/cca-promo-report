"""Loading of report.yaml, retailer YAMLs and environment settings."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

import yaml
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import CountryCode, CountryConfig, ReportConfig, RetailerConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Environment (.env). Proxies per country: HTTPS_PROXY_PA, HTTPS_PROXY_CR, ..."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore", env_ignore_empty=True)

    https_proxy_pa: str | None = None
    https_proxy_cr: str | None = None
    https_proxy_ec: str | None = None
    https_proxy_py: str | None = None
    https_proxy_uy: str | None = None
    headless: bool = True

    def proxy_for(self, country: CountryCode) -> str | None:
        return getattr(self, f"https_proxy_{country.value.lower()}")


class ConfigError(RuntimeError):
    pass


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


class Project:
    """Entry point to every configured object, rooted at a project directory."""

    def __init__(self, root: Path = PROJECT_ROOT) -> None:
        self.root = root
        self.config_dir = root / "config"
        self.runs_dir = root / "runs"

    @cached_property
    def report(self) -> ReportConfig:
        path = self.config_dir / "report.yaml"
        try:
            return ReportConfig.model_validate(_load_yaml(path))
        except ValidationError as exc:
            raise ConfigError(f"{path}:\n{exc}") from exc

    @cached_property
    def retailers(self) -> list[RetailerConfig]:
        out: list[RetailerConfig] = []
        errors: list[str] = []
        for path in sorted((self.config_dir / "retailers").glob("*.yaml")):
            data = _load_yaml(path)
            data["slug"] = path.stem
            try:
                out.append(RetailerConfig.model_validate(data))
            except ValidationError as exc:
                errors.append(f"{path.name}:\n{exc}")
        if errors:
            raise ConfigError("\n\n".join(errors))
        order = {c.code: i for i, c in enumerate(self.report.countries)}
        return sorted(out, key=lambda r: (order.get(r.country, 99), r.slug))

    def country(self, code: CountryCode) -> CountryConfig:
        for c in self.report.countries:
            if c.code == code:
                return c
        raise ConfigError(f"country {code} not in report.yaml")

    def select(self, countries: list[str] | None = None, retailers: list[str] | None = None) -> list[RetailerConfig]:
        wanted_c = {c.upper() for c in countries or []}
        wanted_r = {r.lower() for r in retailers or []}
        if not wanted_c and not wanted_r:
            return list(self.retailers)
        # union: "-c PA -r uy_covercompany" = every PA retailer plus Cover Company
        return [
            r
            for r in self.retailers
            if r.country.value in wanted_c or r.slug in wanted_r or r.retailer_name.lower() in wanted_r
        ]

    def run_dir(self, name: str) -> Path:
        path = self.runs_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path
