# Contributing

Issues and pull requests are welcome. This is a hobby project maintained in spare time, so replies may be slow.

- **Bugs:** include your Narratarr version, slskd version, and the job's log from *Queue → Details*.
- **Matching problems:** a screenshot of the candidate list (scores and reasons) is the most useful thing you can send.
- **Code:** run `python tests/e2e.py` before opening a PR. It starts mock Audible, Audnexus, Hardcover, slskd and Audiobookshelf services, so no real accounts are needed. New behaviour should come with a scenario there.
- Keep the frontend build-free (plain JS/CSS in `web/`).
