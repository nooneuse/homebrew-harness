---
name: text-summary
description: Summarize local documents or text concisely, preserving the objective, key facts, limitations, and open questions.
---

# Text summarization

**English** | [简体中文](SKILL.zh-CN.md)

1. Read the files selected by the user before organizing the summary. State when content is truncated or information is incomplete.
2. Use three sections: **Objective**, **Key points**, and **Limitations and open questions**.
3. Include at most five key points. Preserve facts that affect understanding or decisions, and avoid repeating rhetoric or promotional language.
4. Distinguish statements in the source from your own inferences. Put unconfirmed details in Limitations and open questions.
5. Return the summary without editing source files unless the current user requests a file artifact.

This skill provides a method for organizing text. It runs no scripts, installs no dependencies, and changes no host permissions.
