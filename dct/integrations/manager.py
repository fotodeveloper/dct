"""Client selection and shared integration lifecycle (no database bootstrap)."""

from __future__ import annotations


def get_integration(name: str):
    """Load only the requested adapter, so optional clients stay optional."""
    if name == "claude":
        from dct.integrations.claude import ClaudeIntegration
        return ClaudeIntegration()
    if name == "codex":
        from dct.integrations.codex import CodexIntegration
        return CodexIntegration()
    raise ValueError(f"Unknown integration: {name}")


def integration_status(adapter) -> dict:
    checks = adapter.doctor()
    return {"client": adapter.name, "detected": adapter.detect(),
            "ok": not any(c["status"] == "error" for c in checks), "checks": checks}


def install_integration(adapter) -> dict:
    """Install in dependency order; a failed step is reported, never hidden."""
    steps = {}
    for name in ("install_mcp", "install_skills", "install_instructions"):
        steps[name] = getattr(adapter, name)()
    install_hooks = getattr(adapter, 'install_hooks', None)
    if callable(install_hooks):
        steps['install_hooks'] = install_hooks()
    return {"client": adapter.name, "steps": steps, **integration_status(adapter)}


def refresh_integrations() -> list[dict]:
    """Refresh detected integrations after upgrade, preserving instruction opt-in.

    Unlike an explicit install/repair, upgrade does not introduce managed
    instruction blocks or wire new hooks. Existing legacy entry points remain
    the implementation used by the Claude adapter.

    Each result: ``{"client", "ok", "removed", "checks", "remedies"}`` plus
    ``"error"`` (the failing checks' messages) when not ok, and for Claude
    ``"reference"`` (the ``install_reference`` result, K-66). ``remedies``
    lists, per failing check, its own remedy when the check carries one
    (a foreign reference must be moved aside first — a repair would only
    preserve it again), otherwise the client's ``dct integrate repair``.
    """
    results = []
    for name in ("claude", "codex"):
        adapter = get_integration(name)
        try:
            if not adapter.detect():
                continue
            extra = {}
            if name == "claude":
                from dct.integrations import claude
                claude.install_skills()
                claude.install_hook()
                # K-36: the reference the CLAUDE.md block points at is an
                # asset, refreshed with skills/hooks — not instruction opt-in.
                extra["reference"] = claude.install_reference()
                removed = claude.prune_dangling_symlinks()
                if claude.CLAUDE_MD.is_file() and claude.SNIPPET_START in claude.CLAUDE_MD.read_text(encoding="utf-8"):
                    claude.attach_claude_md_snippet()
            else:
                from dct.integrations.codex import START
                adapter.install_skills()
                removed = []
                if adapter.instructions_path.is_file() and START in adapter.instructions_path.read_text(encoding="utf-8"):
                    adapter.install_instructions()
                refresh_hooks = getattr(adapter, 'refresh_hooks', None)
                if callable(refresh_hooks):
                    refresh_hooks()
            # Upgrade refreshes opted-in assets, not the complete integration.
            # Missing instruction blocks or hook wiring are not upgrade failures.
            asset_names = {"skills", "hook_assets"}
            if name == "claude":
                asset_names.add("reference")
            if name == "codex":
                asset_names.update({"hooks", "hooks_trust", "hook_dependencies"})
            asset_checks = [check for check in adapter.doctor()
                            if check["check"] in asset_names
                            or check["check"].startswith("skill:")]
            failing = [check for check in asset_checks if check["status"] == "error"]
            remedies = []
            for check in failing:
                remedy = check.get("remedy") or f"Run `dct integrate repair {name}`."
                if remedy not in remedies:
                    remedies.append(remedy)
            result = {"client": name, "ok": not failing, "removed": removed, "checks": asset_checks,
                      "remedies": remedies, **extra}
            if failing:
                result["error"] = "; ".join(check["message"] for check in failing)
            results.append(result)
        except Exception as exc:  # noqa: BLE001 — report per-client failure, continue others
            results.append({"client": name, "ok": False, "error": str(exc)})
    return results
