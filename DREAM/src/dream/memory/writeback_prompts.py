"""Prompts for bounded platform publication artifacts."""

CHARACTER_WRITEBACK_PROMPT = """Compress the supplied AI decision rules into a
stable Character Definition. No user profile is provided: keep every rule
user-agnostic, lead with the most stable behavior, and return no unsupported
facts. Stay within the requested character limit."""

USER_PERSONA_WRITEBACK_PROMPT = """Compress the supplied evidence-backed user
profile into a User Persona that can improve future service. Preserve only
stable facts or preferences supported by the profile and invent nothing. Stay
within the requested character limit."""
