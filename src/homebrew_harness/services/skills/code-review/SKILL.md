---
name: code-review
description: Review Python code for concrete defects, edge cases, and validation evidence; use read-only review by default and a consistent report structure.
---

# Python code review

**English** | [简体中文](SKILL.zh-CN.md)

1. Establish the files and scope to review, then read their implementation with file tools. Inspect callers or existing tests when needed.
2. Prioritize issues that cause incorrect behavior, such as empty-input handling, out-of-range access, unreleased resources, and missing error handling. Distinguish confirmed defects from questions requiring more context.
3. Review without editing by default. Edit only when the current user explicitly requests changes; command execution remains subject to host approval.
4. Include three sections in the report: **Conclusion**, **Findings**, and **Validation**.
5. Each finding includes severity, file location, trigger, impact, and recommendation. Base locations and line numbers on content actually read.
6. If no confirmed defects are found, say so and describe the review scope. If tests were not run, state "Tests not run" in Validation.

These steps are a working guide. Current user requirements and host permissions take precedence; loading this skill grants no additional tool permissions.
