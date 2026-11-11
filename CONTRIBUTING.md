# Contributing

[简体中文](CONTRIBUTING.zh-CN.md)

Use a branch and a pull request for ordinary changes, whether written by a person or developed with an agent. Describe the problem, resulting behavior and relevant validation. Keep each contribution focused so reviewers can assess it independently.

```sh
git switch -c feature/describe-the-change
# Make and validate the change.
git push -u origin feature/describe-the-change
```

Open a PR against the project's main branch. Follow review feedback and wait for the required checks and approvals before merging through the platform. Use the repository's allowed merge method. New head commits may require another review or CI pass.

Repository owners configure PR requirements and branch protection for human and service accounts. Agent-assisted work uses the contributor's configured identity; it does not require an agent co-author trailer. An autonomous maintenance service uses its registered service identity and the same PR workflow.

Install the package in an isolated Python environment and run tests appropriate to the change. The full local suite is:

```sh
python -m unittest discover -s tests -t .
```

Some integration checks need an explicitly configured container host. Include the checks you ran and any relevant unverified cases in the PR. Keep credentials, private connection values, personal development records and runtime data outside the repository. Public documentation defaults to English and links to its Chinese counterpart.

For supervised maintenance, see [Repository delivery](docs/DELIVERY.md). Its default waits for a maintainer to merge; optional service execution requires a fixed, approved native merge plan. Research, implementation planning and verification process documents remain development notes rather than project documentation.
