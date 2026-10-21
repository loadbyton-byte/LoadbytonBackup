# Source-market city catalogue

`leadengine/markets/cities.psv` lists 231 cities in 105 countries (South Asia, GCC, Middle East & Caucasus, Africa, Europe, Russia &
Central Asia, East & Southeast Asia, Oceania, North America, Latin America & Caribbean, plus UAE home markets). Each row has the ISO
country, region, IANA timezone, marketing languages (ISO 639-1), an editorial tier, and aliases (Bangalore/Bengaluru, Chennai/Madras,
Mumbai/Bombay, Arabic and Russian names for some).

- **Tier is editorial, not data.** It is a starting priority for campaign planning. Replace it with demand measured from your own
  analytics, ad lead forms and portal origin statistics.
- **Currencies** follow the country's current currency at the time of writing; verify before quoting prices in them.
- **Add a city:** append a pipe-separated row. The loader rejects unknown countries/timezones, bad language codes, duplicate ids and
  any name or alias already used by another city (run `python -m pytest tests/test_cities.py`).
- **Lookup:** `resolve("Bengaluru")`, `resolve("chennari", fuzzy=True)`, `find_cities(text)` (free text, EN/AR/RU, longest match wins),
  `search(...)`. API: `GET /markets/cities?q=&country=&region=&tier=&lang=&limit=`, `GET /markets/summary`.
- Text matching ignores 1-2 character aliases and uses word boundaries for Latin names to avoid false hits ("Nice" is deliberately absent).
- This is a catalogue of *places*. It contains no personal data, and nothing here collects any.
