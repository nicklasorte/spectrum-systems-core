# Commit message hygiene — never spell out CI skip tokens

GitHub Actions silently skips ALL workflows on the head commit of a push
or PR when the head commit's title OR body contains any of these literal
substrings (the match is naive — backticks, code fences, and quotes do
NOT escape it):

- the bracketed `skip ci` token
- the bracketed `ci skip` token
- the bracketed `no ci` token
- the bracketed `skip actions` token
- the bracketed `actions skip` token

When the token lands in a head-commit message, `pytest` and `smoke-test`
never fire; the PR shows `pending` with zero check_runs and zero
statuses — easy to misdiagnose as "the approval gate" or "Actions is
slow" because GitHub does not emit an explanatory message.

This is a real foot-gun because several workflows in this repo
(`validate-and-baseline.yml`, `debug-single-transcript.yml`) intentionally
EMIT skip-ci commits to break loops, and describing that behaviour in
prose makes it tempting to paste the literal token.

## Rule

When documenting these workflows in commit messages or PR TITLES, refer
to the token without the literal brackets. Acceptable forms: "the
skip-ci marker", "GitHub's skip-ci token", "skip-ci (bracketed)", or
`[ skip ci ]` with internal spaces. The same rule applies to PR titles
because the merge commit on `main` inherits the title. Inside fenced
code blocks in a PR BODY the token is safe — GitHub only inspects
commit messages, not PR bodies — but never inside the title or any
commit message in the chain.

## Diagnosis

If you discover a PR whose checks are mysteriously empty, run:

```bash
git log -1 --format='%B' | grep -nE '\[skip ci\]|\[ci skip\]|\[no ci\]|\[skip actions\]|\[actions skip\]'
```

A non-empty grep means the head commit message is the cause. Fix by
pushing a follow-up commit whose message omits the token (the new HEAD
re-triggers via the `synchronize` event) or by amending the commit
message and force-pushing the branch.
