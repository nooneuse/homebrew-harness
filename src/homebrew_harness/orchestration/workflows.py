"""Host-registered workflows: bounded review of explicitly supplied changes."""

from homebrew_harness.orchestration.workflow_runtime import (
    RunnerOutput,
    WorkflowRegistry,
    WorkflowSpec,
)

FINDING = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "severity", "evidence"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 600},
    },
}
FINDINGS = {
    "type": "object",
    "required": ["findings"],
    "additionalProperties": False,
    "properties": {"findings": {"type": "array", "maxItems": 2, "items": FINDING}},
}
REVIEW_ARGS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["changes"],
    "properties": {
        "changes": {
            "type": "string",
            "minLength": 1,
            "maxLength": 12_000,
            "pattern": r"\S",
        },
        "dimensions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "enum": ["correctness", "security", "performance"],
            },
        },
    },
}


async def review_changes(ctx, args):
    dimensions = args.get("dimensions", ["correctness", "security"])
    ctx.phase("Review")
    audit_saturation = []

    async def audit(_value, dimension, _index):
        return await ctx.agent(
            "Review the supplied changes for "
            + dimension
            + " issues. Report at most two concrete findings "
            "with exact evidence in these changes, or an empty list. Do not invent surrounding code.\n\n"
            + args["changes"],
            schema=FINDINGS,
            label="audit:" + dimension,
            phase="Review",
        )

    async def verify(audited, dimension, _index):
        findings = audited["findings"]
        if len(findings) >= 2:
            audit_saturation.append(dimension)
        if not findings:
            return []
        ctx.phase("Verify")
        verdict = {
            "type": "object",
            "additionalProperties": False,
            "required": ["is_real", "reason"],
            "properties": {
                "is_real": {"type": "boolean"},
                "reason": {"type": "string", "maxLength": 400},
            },
        }
        keys = [str(index) for index in range(len(findings))]
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": keys,
            "properties": {key: verdict for key in keys},
        }
        from homebrew_harness.orchestration.workflow_runtime import json_text

        results = await ctx.agent(
            "Adversarially verify these "
            + dimension
            + " findings against the supplied changes. "
            "Mark is_real only when the supplied code supports the finding, and give a concise reason. "
            "The JSON keys identify finding indices.\nChanges:\n"
            + args["changes"]
            + "\nFindings:\n"
            + json_text(findings),
            schema=schema,
            label="verify:" + dimension,
            phase="Verify",
        )
        return [
            {
                **finding,
                "dimensions": [dimension],
                "verification": results[str(index)]["reason"],
            }
            for index, finding in enumerate(findings)
            if results[str(index)]["is_real"]
        ]

    groups = await ctx.pipeline(dimensions, audit, verify)
    merged = {}
    for group in groups:
        for finding in group:
            key = (finding["title"].casefold(), finding["evidence"])
            if key in merged:
                prior = merged[key]
                prior["dimensions"].extend(finding["dimensions"])
                severity = {"high": 0, "medium": 1, "low": 2}
                if severity[finding["severity"]] < severity[prior["severity"]]:
                    prior["severity"] = finding["severity"]
            else:
                merged[key] = finding
    confirmed = sorted(
        merged.values(),
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}[item["severity"]],
            item["title"],
        ),
    )
    ctx.log("Confirmed " + str(len(confirmed)) + " findings from supplied changes.")
    import hashlib

    saturated = sorted(audit_saturation)
    return {
        "confirmed": confirmed,
        "reviewed_dimensions": dimensions,
        "coverage": {
            "scope": "explicit_changes_only",
            "input_characters": len(args["changes"]),
            "input_sha256": hashlib.sha256(args["changes"].encode()).hexdigest(),
            "surrounding_repository_reviewed": False,
            "finding_limit_per_dimension": 2,
            "possible_saturation": saturated,
            "complete_repository_review": False,
        },
    }


def default_registry():
    return WorkflowRegistry(
        [
            WorkflowSpec(
                "review-changes",
                "Review explicit args.changes, then verify and merge findings; optional args.dimensions selects correctness/security/performance.",
                review_changes,
                REVIEW_ARGS,
                ("Review", "Verify"),
            )
        ]
    )


DEMO_CHANGES = "def divide(total, count):\n    return total / count\n"


class DemoWorkflowRunner:
    """Explicit offline demonstration; its cache cannot be resumed by real models."""

    identity = "offline-demo-v1"

    def run(self, prompt, schema, label, max_tokens):
        if label.startswith("audit:"):
            value = (
                {
                    "findings": [
                        {
                            "title": "Zero count raises ZeroDivisionError",
                            "severity": "medium",
                            "evidence": "return total / count",
                        }
                    ]
                }
                if label == "audit:correctness"
                else {"findings": []}
            )
        else:
            value = {
                key: {
                    "is_real": True,
                    "reason": "The supplied demo code divides by count without checking zero.",
                }
                for key in schema["required"]
            }
        return RunnerOutput(value)
