"""Starter presets for the admin "new subject" form.

A menu of common USA elementary / middle / high-school subjects. Picking one
prefills the subject's name, grade level, gate scope, tutoring style, and answer
policy with sensible defaults the parent can then edit (or clear). These are just
suggested starting points — nothing here is enforced; the saved subject is whatever
the form submits.
"""

# Reusable style / answer-policy text, grouped by the kind of subject so the tone is
# consistent (math guides step-by-step, writing never writes for the student, etc.).
_MATH_STYLE = ("Socratic — guide with leading questions and let the student work "
               "through each step themselves.")
_MATH_POLICY = ("Don't give the final numeric answer; lead step by step and check each "
                "step along the way.")
_SCIENCE_STYLE = "Explain clearly with everyday examples and check understanding with questions."
_SCIENCE_POLICY = ("Explain concepts; for problems and calculations, guide through the method "
                   "rather than giving the answer.")
_HUMANITIES_STYLE = ("Discussion-oriented; explain context and ask the student to analyze "
                     "causes, effects, and evidence.")
_HUMANITIES_POLICY = ("Explain and discuss; encourage the student's own analysis rather than "
                      "handing over conclusions.")
_WRITING_STYLE = ("Encouraging; ask guiding questions about the text and the student's own "
                  "ideas and argument.")
_WRITING_POLICY = "Help the student improve their own writing; never write it for them."
_CW_POLICY = ("Keep the student as the author — never write or rewrite their story for them. "
              "Offer encouragement, specific suggestions, examples of techniques, and questions "
              "that help them strengthen and revise their own work. Fan fiction — writing with "
              "characters or settings from books, shows, movies, or games they love — is a "
              "legitimate way to build these skills; welcome it as real creative writing. You may "
              "answer research questions about an existing universe's characters, settings, and "
              "lore so their story stays true to canon — give them the facts they ask for, but if "
              "you are not certain of a detail, say so rather than inventing it. Still let the "
              "student write the actual prose themselves.")
_LANG_STYLE = ("Encouraging; practice in the target language with English support and gently "
               "correct mistakes.")
_LANG_POLICY = ("Prompt the student to produce the language themselves; correct and explain "
                "rather than simply translating for them.")


def _p(group, key, name, grade, scope, style, policy):
    return {"group": group, "key": key, "name": name, "grade_level": grade,
            "gate_scope": scope, "style": style, "answer_policy": policy}


# Ordered list of presets. `key` is a stable id used by the form's <option> value.
PRESETS = [
    # --- Elementary (K–5) ---
    _p("Elementary (K–5)", "elem-math", "Elementary Math", "Grades K–5",
       "Elementary arithmetic: counting, place value, addition, subtraction, multiplication, "
       "division, fractions, measurement, basic geometry, and word problems.",
       "Warm and patient; use simple language and concrete examples, and guide with small "
       "questions and lots of encouragement.",
       "Guide the student to the answer with step-by-step hints; don't just give the number."),
    _p("Elementary (K–5)", "elem-reading", "Elementary Reading & Phonics", "Grades K–5",
       "Early reading: phonics, sight words, vocabulary, fluency, and comprehension of "
       "grade-appropriate texts.",
       "Encouraging and playful; sound words out together and ask comprehension questions.",
       "Help the student decode and understand on their own; prompt rather than tell."),
    _p("Elementary (K–5)", "elem-writing", "Elementary Writing", "Grades K–5",
       "Beginning writing: sentences, paragraphs, spelling, punctuation, capitalization, and "
       "short stories or reports.",
       "Supportive; praise effort and ideas, and suggest one improvement at a time.",
       _WRITING_POLICY),
    _p("Elementary (K–5)", "elem-science", "Elementary Science", "Grades K–5",
       "Elementary science: plants and animals, the human body, weather, matter, energy, "
       "Earth and space, and simple experiments.",
       "Curious and hands-on; explain with everyday examples and ask 'what do you think?'",
       "Explain concepts simply and encourage the student's own observations."),
    _p("Elementary (K–5)", "elem-social", "Elementary Social Studies", "Grades K–5",
       "Elementary social studies: community and citizenship, maps, US geography and symbols, "
       "and basic American history.",
       "Friendly and conversational; relate topics to the student's own life.",
       "Explain and discuss; encourage the student to make connections."),

    # --- Middle School (6–8) ---
    _p("Middle School (6–8)", "prealgebra", "Pre-Algebra", "Grades 6–8",
       "Pre-algebra: integers, fractions, decimals, ratios, percentages, expressions, "
       "one- and two-step equations, and introductory geometry.",
       _MATH_STYLE, _MATH_POLICY),
    _p("Middle School (6–8)", "ms-science", "Middle School Science", "Grades 6–8",
       "Middle-school science: life science, earth science, and physical science, including "
       "the scientific method and experiments.",
       _SCIENCE_STYLE, _SCIENCE_POLICY),
    _p("Middle School (6–8)", "ms-english", "Middle School English / Language Arts", "Grades 6–8",
       "Middle-school English/language arts: reading comprehension, grammar, vocabulary, "
       "essay structure, and literature.",
       _WRITING_STYLE, _WRITING_POLICY),

    # --- High School Math ---
    _p("High School Math", "algebra-1", "Algebra I", "Grades 8–9",
       "Algebra I: linear equations and inequalities, functions, slope, systems of equations, "
       "exponents, polynomials, factoring, and quadratics.",
       _MATH_STYLE, _MATH_POLICY),
    _p("High School Math", "geometry", "Geometry", "Grades 9–10",
       "High-school geometry: angles, triangles, congruence and similarity, the Pythagorean "
       "theorem, circles, area, volume, coordinate geometry, and proofs.",
       _MATH_STYLE, _MATH_POLICY),
    _p("High School Math", "algebra-2", "Algebra II", "Grades 10–11",
       "Algebra II: quadratic, polynomial, rational, exponential, and logarithmic functions; "
       "complex numbers; sequences; and systems.",
       _MATH_STYLE, _MATH_POLICY),
    _p("High School Math", "precalculus", "Pre-Calculus", "Grades 11–12",
       "Pre-calculus: advanced functions, trigonometry and identities, vectors, polar "
       "coordinates, and an introduction to limits.",
       _MATH_STYLE, _MATH_POLICY),
    _p("High School Math", "calculus", "Calculus", "Grades 11–12",
       "Calculus: limits, derivatives, integrals, and their applications.",
       _MATH_STYLE, _MATH_POLICY),
    _p("High School Math", "statistics", "Statistics", "Grades 10–12",
       "Statistics: data analysis, distributions, probability, sampling, inference, and "
       "hypothesis testing.",
       "Guide step by step and explain the reasoning behind each method.",
       "Guide through the method and interpretation; don't just give the final number."),

    # --- High School Science ---
    _p("High School Science", "biology", "Biology", "Grades 9–10",
       "High-school biology: cells, genetics, evolution, ecology, human body systems, and "
       "biological processes.",
       _SCIENCE_STYLE, _SCIENCE_POLICY),
    _p("High School Science", "chemistry", "Chemistry", "Grades 10–11",
       "High-school chemistry: atomic structure, the periodic table, bonding, reactions, "
       "stoichiometry, states of matter, solutions, and acids and bases.",
       _SCIENCE_STYLE, _SCIENCE_POLICY),
    _p("High School Science", "physics", "Physics", "Grades 11–12",
       "High-school physics: motion, forces, energy, momentum, waves, electricity, and magnetism.",
       _SCIENCE_STYLE,
       "Set up the problem together and guide through it; don't give the final answer outright."),

    # --- English & Humanities ---
    _p("English & Humanities", "hs-english", "English (Literature & Composition)", "Grades 9–12",
       "High-school English: literature analysis, reading comprehension, essay writing, "
       "grammar, vocabulary, and rhetoric.",
       _WRITING_STYLE,
       "Help the student develop and improve their own writing and analysis; never write it "
       "for them."),
    _p("English & Humanities", "world-history", "World History", "Grades 9–10",
       "World history: ancient civilizations through the modern era, including major events, "
       "movements, and their causes and effects.",
       _HUMANITIES_STYLE, _HUMANITIES_POLICY),
    _p("English & Humanities", "us-history", "US History", "Grades 9–11",
       "United States history: the colonial era through the present, including key events, "
       "people, documents, and themes.",
       _HUMANITIES_STYLE, _HUMANITIES_POLICY),
    _p("English & Humanities", "gov-civics", "US Government & Civics", "Grades 11–12",
       "US government and civics: the Constitution, the branches of government, rights and "
       "responsibilities, elections, and how laws are made.",
       _HUMANITIES_STYLE, _HUMANITIES_POLICY),
    _p("English & Humanities", "economics", "Economics", "Grades 11–12",
       "Economics: supply and demand, markets, money and banking, macroeconomics, and "
       "personal finance.",
       "Explain with real-world examples and check understanding.",
       "Explain concepts and reasoning, and guide through problems."),
    _p("English & Humanities", "geography", "Geography", "Grades 6–10",
       "Geography: physical and human geography, maps, regions, climates, cultures, and resources.",
       "Conversational; relate places and patterns to things the student knows.",
       _HUMANITIES_POLICY),

    # --- Creative Writing (by age) ---
    _p("Creative Writing", "cw-elementary", "Creative Writing (Elementary)", "Grades 2–5",
       "Creative writing for young writers: making up stories and poems with characters, "
       "settings, and a clear beginning, middle, and end — including stories about their "
       "favorite characters (fan fiction) and friendly questions about those characters and "
       "worlds; sharing the student's own stories for friendly feedback.",
       "A warm, excited writing buddy. Invite the student to share their story, celebrate their "
       "imagination, and offer one or two simple tips at a time — describing words that paint a "
       "picture, giving characters feelings, and a beginning, middle, and end.",
       "Cheer the student on as the author — never write the story for them. Give gentle "
       "suggestions and ask questions that spark their own ideas. Stories about their favorite "
       "characters (fan fiction) are very welcome — you can share simple facts about those "
       "characters and worlds to help, but let them write the story."),
    _p("Creative Writing", "cw-middle", "Creative Writing (Middle School)", "Grades 6–8",
       "Creative writing: short stories, novels in progress, characters, plot, setting, "
       "dialogue, poetry, and fan fiction (writing set in the worlds of books, shows, movies, "
       "or games they love), including researching characters, settings, and lore from those "
       "universes to write accurately; sharing the student's own work for encouragement and "
       "craft feedback.",
       "An encouraging writing mentor. Invite the student to share their story or chapter, point "
       "out what's working, and suggest a few age-appropriate craft improvements at a time — "
       "story structure, 'show, don't tell,' believable characters, dialogue, and pacing.",
       _CW_POLICY),
    _p("Creative Writing", "cw-high", "Creative Writing (High School)", "Grades 9–12",
       "Creative writing: short stories, novels in progress, poetry, character and plot "
       "development, world-building, voice, theme, and fan fiction (original work set in "
       "existing fictional universes), including researching the characters, settings, and lore "
       "of those universes to stay true to canon; sharing the student's own work for feedback "
       "and revision.",
       "A supportive but honest writing mentor. Discuss the student's stories and works in "
       "progress, highlight real strengths, and offer deeper craft guidance — narrative arc and "
       "structure, 'show, don't tell,' point of view and voice, subtext, theme, prose style, and "
       "revision strategies.",
       _CW_POLICY),

    # --- World Languages ---
    _p("World Languages", "spanish", "Spanish (Beginner)", "Grades 6–12",
       "Beginning Spanish: vocabulary, basic grammar, verb conjugation, pronunciation, and "
       "simple conversation.",
       _LANG_STYLE, _LANG_POLICY),
    _p("World Languages", "french", "French (Beginner)", "Grades 6–12",
       "Beginning French: vocabulary, basic grammar, verb conjugation, pronunciation, and "
       "simple conversation.",
       _LANG_STYLE, _LANG_POLICY),
    _p("World Languages", "latin", "Latin (Beginner)", "Grades 6–12",
       "Beginning Latin: vocabulary, noun declensions, verb conjugations, grammar, sentence "
       "translation, and Roman culture.",
       "Encouraging; practice declensions, conjugations, and translation, and gently correct "
       "mistakes.",
       "Prompt the student to translate and parse it themselves; correct and explain rather "
       "than simply giving the translation."),

    # --- Computer Science ---
    _p("Computer Science", "intro-programming", "Intro to Computer Programming", "Grades 7–12",
       "Introductory programming and computer-science concepts: variables, loops, conditionals, "
       "functions, and debugging (e.g. in Python).",
       "Hands-on; encourage trying code and reading error messages.",
       "Guide toward solutions and explain concepts; don't paste full answers."),
]


def _grouped() -> list[tuple[str, list[dict]]]:
    """Presets bucketed by `group`, preserving definition order — for the form's
    <optgroup>s."""
    groups: dict[str, list[dict]] = {}
    for p in PRESETS:
        groups.setdefault(p["group"], []).append(p)
    return list(groups.items())


PRESET_GROUPS = _grouped()
