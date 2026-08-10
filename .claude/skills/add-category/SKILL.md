---
name: add-category
description: Add a new spending/income category to finappa. Use when the user wants a new category available for transaction classification.
---

# Add a category

`app/categories.py::CategoryName` is the single source of truth (spec §5.1) — it
both seeds the `categories` table and constrains what the LLM is allowed to
return.

1. Add a member to `CategoryName` in `app/categories.py`:
   ```python
   class CategoryName(StrEnum):
       ...
       PETS = "Pets"
   ```
2. No migration needed — `app/db.py::init_db` seeds any category present in the
   enum but missing from the table, and it runs on every startup.
3. Restart the bot (`docker compose up -d --build` in production, or just rerun
   locally) so the new enum value reaches the LLM's schema.
4. Do not remove a category that has existing transactions or merchants pointing
   at it — `category_id` is a required foreign key with no cascade. If a category
   is truly obsolete, leave the enum member in place; it just stops being offered
   for *new* extractions once removed, which would orphan history. Removing one
   safely is out of scope for this skill — ask the user before doing it, since it
   requires reassigning existing rows first.
