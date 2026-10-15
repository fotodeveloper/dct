**Trailers** follow the project's stored preferences from the Step 1
`list_projects` row, applied without a question:

- `commit_co_authored_by` true → a `Co-Authored-By:` trailer (the actual host-supplied identity); false → none.
- `commit_session_link` true → the session link the client gives for this
  session (only its real host-supplied link); false → none. A session
  link is written only on a stored true (yes).
- null = not answered yet (never a "no"): ask ONCE — one {{host.question}}
  before the screen, both preferences in it — store the answers with
  `dct project trailers <slug> --co-authored-by on|off --session-link on|off`
  and apply them; the question never comes again for that project.
