"""Transparency label generation.

Maps an attribution result + confidence score to the plain-language label a
reader sees. The three variants are the verbatim text specified in
planning.md > Transparency Label Design. The raw score is never shown; it is
translated into an approximate percentage plus a verbal hedge so the label is
meaningful to a non-technical reader.
"""


def make_label(attribution_result, confidence_score):
    """Return the transparency label text for a classification."""
    pct = round(confidence_score * 100)
    human_pct = round((1 - confidence_score) * 100)

    if attribution_result == "likely_ai":
        return (
            f"🤖 Likely AI-generated. Our analysis suggests this text was probably "
            f"created with the help of AI tools (roughly {pct}% likely AI-generated). "
            f"This is an automated estimate, not a certainty. If you're the creator "
            f"and believe this is wrong, you can appeal this decision."
        )

    if attribution_result == "likely_human":
        return (
            f"✍️ Likely human-written. Our analysis suggests this text was most "
            f"likely written by a person (roughly {human_pct}% likely human-written). "
            f"No strong AI-generation signals stood out. This is an automated "
            f"estimate, not a guarantee."
        )

    # uncertain
    return (
        "❓ Uncertain. Our analysis couldn't confidently determine whether this "
        "text was written by a person or generated with AI — the signals were "
        "mixed, so we are not making a call. Please use your own judgment. If "
        "you're the creator, you're welcome to add context or appeal."
    )


if __name__ == "__main__":
    # Print all three variants to confirm they match planning.md verbatim.
    for result, score in [("likely_ai", 0.87), ("likely_human", 0.23), ("uncertain", 0.55)]:
        print(f"--- {result} (confidence {score}) ---")
        print(make_label(result, score))
        print()
