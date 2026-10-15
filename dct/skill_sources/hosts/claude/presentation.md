- **The screen travels inside the dialog.** What the owner must read before
  answering is the preview of the option it belongs to, at most 15 lines as
  the dialog shows them. The dialog wraps long lines itself, about 60
  characters wide: a paragraph is one line, never broken by hand (a second
  break leaves single words on a line), and a list or table line stays under
  60 characters. Message text written before a dialog is never the screen: it
  can land in the thinking block, which the owner sees only as a one-line
  summary, so no question or option says "shown above". A longer screen is
  split into pages of at most 15 lines: the option's preview shows page 1, and
  one more single-select question, "Details", has one option per page (2 to
  4), each with that page as its preview; its answer is not a decision.

When a user decision is needed, use `AskUserQuestion`. Long text is in previews and Details pages, never in option labels or descriptions. A Details answer is reading, not approval.
