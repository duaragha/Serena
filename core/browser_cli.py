"""Saved browser auth profiles: enroll once, reuse sealed sessions."""

from __future__ import annotations

import json
import sys

import click

from core.browser_profiles import BrowserProfileError, attach, enroll, remove, seal, status


class BrowserGroup(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except BrowserProfileError as exc:
            raise click.ClickException(str(exc)) from exc


@click.group(cls=BrowserGroup)
def browser():
    """Isolated logged-in browser profiles for automations."""


@browser.command()
@click.argument("slug")
@click.argument("start_url")
@click.option("--purpose", default="", help="What this profile is for.")
def enroll_cmd(slug, start_url, purpose):
    """Open an isolated browser for user-typed login (agent watches only)."""
    out = enroll(slug, start_url, purpose=purpose)
    click.echo(json.dumps(out, indent=2))


@browser.command()
@click.argument("slug")
@click.option("--url-prefix", default="", help="Expected post-login URL prefix.")
@click.option("--title-regex", default="", help="Expected post-login title regex.")
def seal_cmd(slug, url_prefix, title_regex):
    """Verify the login marker and seal the profile."""
    out = seal(slug, url_prefix=url_prefix, title_regex=title_regex)
    click.echo(json.dumps(out, indent=2))


@browser.command(name="use")
@click.argument("slug")
def use_cmd(slug):
    """Attach a sealed profile (marker re-verified, confirmation required)."""
    interactive = sys.stdin.isatty()
    out = attach(
        slug,
        confirm_fn=(lambda prompt: click.confirm(prompt, default=False)) if interactive else None,
    )
    click.echo(json.dumps(out, indent=2))


@browser.command()
@click.argument("slug", required=False)
def status_cmd(slug):
    """Metadata-only status for one profile or the whole store."""
    click.echo(json.dumps(status(slug), indent=2))


@browser.command()
@click.argument("slug")
def remove_cmd(slug):
    """Wipe a profile completely (cookies included)."""
    if sys.stdin.isatty() and not click.confirm(f"wipe browser profile '{slug}' completely?", default=False):
        raise click.ClickException("aborted")
    click.echo(json.dumps(remove(slug), indent=2))
