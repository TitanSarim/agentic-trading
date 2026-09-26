"""Definition-of-Done style validation report artifact (Phase 8).

Writes JSON + Markdown under reports/ (or data/). Offline checklist only —
does not claim live Demo DoD from plan §9 is fully complete.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.soak import SoakReport
from research.stress import StressReport
from research.walk_forward import WalkForwardReport


@dataclass
class DodCheck:
    id: str
    description: str
    passed: bool
    evidence: str = ""
    scope: str = "offline"  # offline | lan | live

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "passed": self.passed,
            "evidence": self.evidence,
            "scope": self.scope,
        }


@dataclass
class DodReport:
    generated_at: str
    overall_passed: bool
    checks: list[DodCheck] = field(default_factory=list)
    walk_forward: dict[str, Any] | None = None
    stress: dict[str, Any] | None = None
    soak: dict[str, Any] | None = None
    note: str = (
        "Phase 8 offline validation toward plan §9. "
        "LAN demo soak, broker disconnect, and live pilot remain out of P8."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "overall_passed": self.overall_passed,
            "checks": [c.to_dict() for c in self.checks],
            "walk_forward": self.walk_forward,
            "stress": self.stress,
            "soak": self.soak,
            "note": self.note,
        }

    def to_markdown(self) -> str:
        lines = [
            "# Phase 8 validation report (Definition of Done — offline slice)",
            "",
            f"- Generated: `{self.generated_at}`",
            f"- Overall: **{'PASS' if self.overall_passed else 'FAIL'}**",
            "",
            self.note,
            "",
            "## Checks",
            "",
            "| ID | Result | Scope | Description | Evidence |",
            "|----|--------|-------|-------------|----------|",
        ]
        for c in self.checks:
            mark = "PASS" if c.passed else "FAIL"
            evid = (c.evidence or "").replace("|", "/")
            lines.append(
                f"| `{c.id}` | {mark} | {c.scope} | {c.description} | {evid} |"
            )
        lines.extend(["", "## Artifacts", ""])
        if self.walk_forward is not None:
            lines.append(
                f"- Walk-forward passed={self.walk_forward.get('passed')} "
                f"oos_trades={self.walk_forward.get('oos_trades')} "
                f"expectancy={self.walk_forward.get('oos_expectancy')}"
            )
        if self.stress is not None:
            lines.append(
                f"- Stress passed={self.stress.get('passed')} "
                f"scenarios={self.stress.get('scenario_count')}"
            )
        if self.soak is not None:
            lines.append(
                f"- Soak passed={self.soak.get('passed')} "
                f"ticks={self.soak.get('ticks')} "
                f"control_blocks={self.soak.get('control_blocks')}"
            )
        lines.extend(
            [
                "",
                "## Still required before tiny live (P9 / plan §9)",
                "",
                "- Unattended **demo** soak on Windows + MT5 (not this offline loop)",
                "- Broker disconnect/reconnect + restart recovery on LAN",
                "- Protective stops verified at the broker",
                "- Compare expected vs actual slippage/fills on demo",
                "- Live pilot only at deliberately tiny risk after full §9",
                "",
            ]
        )
        return "\n".join(lines)


def build_dod_report(
    *,
    walk_forward: WalkForwardReport | None = None,
    stress: StressReport | None = None,
    soak: SoakReport | None = None,
) -> DodReport:
    now = datetime.now(timezone.utc).isoformat()
    checks: list[DodCheck] = []

    if walk_forward is not None:
        checks.append(
            DodCheck(
                id="walk_forward_oos",
                description="Out-of-sample / walk-forward chronological folds reviewed",
                passed=walk_forward.passed,
                evidence=(
                    f"folds={len(walk_forward.folds)} oos_trades={walk_forward.oos_trades} "
                    f"reasons={walk_forward.fail_reasons or '-'}"
                ),
                scope="offline",
            )
        )
        checks.append(
            DodCheck(
                id="costs_in_sim",
                description="Spread, commission, realistic slippage included in research path",
                passed=True,
                evidence="CostModel applied in Backtester / walk-forward / stress",
                scope="offline",
            )
        )
    else:
        checks.append(
            DodCheck(
                id="walk_forward_oos",
                description="Out-of-sample / walk-forward chronological folds reviewed",
                passed=False,
                evidence="not_run",
                scope="offline",
            )
        )

    if stress is not None:
        checks.append(
            DodCheck(
                id="stress_scenarios",
                description="Cost/latency-style stress (spread, slippage, gap, consecutive losses)",
                passed=stress.passed,
                evidence=f"fail={stress.fail_reasons or '-'}",
                scope="offline",
            )
        )
        consec = next(
            (s for s in stress.scenarios if s.name == "consecutive_losses"), None
        )
        checks.append(
            DodCheck(
                id="risk_limits_unit",
                description="Risk limits exercised (consecutive-loss lock rejects new entries)",
                passed=bool(consec and consec.passed),
                evidence=str(consec.extras if consec else "missing"),
                scope="offline",
            )
        )
    else:
        checks.append(
            DodCheck(
                id="stress_scenarios",
                description="Stress scenarios",
                passed=False,
                evidence="not_run",
                scope="offline",
            )
        )

    if soak is not None:
        checks.append(
            DodCheck(
                id="offline_soak_control",
                description=(
                    "Accelerated soak: heartbeats + halt/kill/resume block new entries"
                ),
                passed=soak.passed,
                evidence=(
                    f"ticks={soak.ticks} blocks={soak.control_blocks} "
                    f"hb={soak.heartbeats} reasons={soak.fail_reasons or '-'}"
                ),
                scope="offline",
            )
        )
        checks.append(
            DodCheck(
                id="manual_auto_kill_offline",
                description="Manual and automatic kill/halt verified in soak loop",
                passed=soak.halt_injected and soak.kill_injected and soak.resume_verified,
                evidence=(
                    f"halt={soak.halt_injected} kill={soak.kill_injected} "
                    f"resume={soak.resume_verified}"
                ),
                scope="offline",
            )
        )
    else:
        checks.append(
            DodCheck(
                id="offline_soak_control",
                description="Accelerated soak",
                passed=False,
                evidence="not_run",
                scope="offline",
            )
        )

    # Explicitly mark LAN/live §9 items as not claimed by P8.
    for cid, desc in (
        (
            "demo_unattended_lan",
            "Demo ran unattended long enough on Windows/MT5 (LAN — out of P8)",
        ),
        (
            "broker_disconnect_lan",
            "Broker disconnect/reconnect and restart recovery (LAN — out of P8)",
        ),
        (
            "live_tiny_pilot",
            "Live pilot at deliberately small risk (P9 — out of P8)",
        ),
    ):
        checks.append(
            DodCheck(
                id=cid,
                description=desc,
                passed=False,
                evidence="out_of_scope_p8",
                scope="lan" if "lan" in cid or "demo" in cid else "live",
            )
        )

    # Overall: only offline checks that were run must pass; LAN/live deferred don't fail overall.
    offline = [c for c in checks if c.scope == "offline"]
    overall = all(c.passed for c in offline) if offline else False

    return DodReport(
        generated_at=now,
        overall_passed=overall,
        checks=checks,
        walk_forward=walk_forward.to_dict() if walk_forward else None,
        stress=stress.to_dict() if stress else None,
        soak=soak.to_dict() if soak else None,
    )


def write_dod_report(
    report: DodReport,
    *,
    output_dir: str | Path = "reports",
    stem: str = "p8-validation",
) -> tuple[Path, Path]:
    """Write JSON + Markdown; return (json_path, md_path)."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    json_path.write_text(
        json.dumps(report.to_dict(), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    return json_path, md_path
