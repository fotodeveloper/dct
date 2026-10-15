## Type triggers — natural language → item_type

Map the phrase the user actually used to the closest type. No type cue in
the phrase → `todo` (the most neutral bucket), without a question. Ask about
the type only when the phrase is genuinely ambiguous — it carries cues for two
different types ("a bug, or maybe a new feature?") — and the choice changes
the scope of the work; otherwise pick the closer type and write.

| User says (EN / PL) | item_type | When to pick |
|---|---|---|
| "problem", "bug", "issue", "crash", "broken", "error", "doesn't work", "regression" / "problem", "błąd", "bug", "crash", "nie działa", "popsute", "zepsute" | **issue** | Observable defect in existing code/system. Reproducible wrong behavior. |
| "todo", "task", "action item", "need to", "must do" / "todo", "zadanie", "zrobić", "do zrobienia", "trzeba" | **todo** | Concrete work that's neither a bug nor a new user-visible capability. Generic "stuff to do". |
| "feature", "add X", "new capability", "let's build", "we should have" / "feature", "ficzer", "funkcja", "dodajmy", "zbudujmy", "nowa funkcja" | **feature** | New user-visible capability that doesn't exist yet. |
| "idea", "maybe", "what if", "consider", "brainstorm", "explore" / "pomysł", "idea", "a może", "rozważ", "zastanów się", "pomyślmy" | **idea** | Exploratory — not yet decided to build. May become a feature later. |
| "improvement", "refactor", "polish", "cleanup", "tidy up", "make X nicer" / "ulepszenie", "poprawa", "poprawmy", "polerka", "cleanup", "refactor", "uporządkuj" | **improvement** | Existing thing works but could be better (refactor, UX polish, perf, docs). |

### Examples

| Phrase | item_type | Why |
|---|---|---|
| "dodaj jako problem że parser się wywala na pustych plikach" | issue | "problem" + observable broken behavior |
| "dodaj ficzer, chcę zrobić dark mode" | feature | "ficzer" + new capability |
| "a może pomyślmy nad wtyczkami?" | idea | "a może" + exploratory |
| "trzeba uporządkować ten moduł parsera" | improvement | "uporządkować" + existing code |
| "dodaj todo — wysłać PR do review" | todo | explicit "todo", generic task |

