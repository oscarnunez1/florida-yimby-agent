Extract structured fields from this Florida YIMBY development article.

Return ONLY a JSON object with these four fields. No explanation, no markdown fences, nothing else.

Fields:
- project_name: the building or development project name (e.g. "Okan Tower", "One Brickell City Centre")
- address: the street address of the project, including street number and name (e.g. "555 North Miami Avenue"); omit city/state
- developer: the developer or ownership company name (e.g. "Related Group", "Swire Properties")
- architect: the architecture or design firm name (e.g. "Arquitectonica", "Kobi Karp"); null if not mentioned

Use null for any field not present in the article.

Example output:
{"project_name": "Okan Tower", "address": "555 North Miami Avenue", "developer": "Okan Group", "architect": "Behar Font & Partners"}
