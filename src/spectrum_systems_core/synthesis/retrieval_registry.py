"""RetrievalRegistry: built-in versioned retrieval recipes for synthesis.

Recipes are deterministic. promoted_only is true on every source by design
(FINDING-F-002). Adding a new recipe means adding an entry to
BUILT_IN_RECIPES — no other config layer.
"""
from __future__ import annotations

BUILT_IN_RECIPES: dict[str, dict] = {
    "default_report_v1": {
        "recipe_id": "default_report_v1",
        "recipe_version": "1.0.0",
        "description": (
            "Standard report bundle: top claims, promoted stories, themes."
        ),
        "sources": [
            {
                "source_type": "technical_claim",
                "max_items": 10,
                "promoted_only": True,
                "filter": "status==evidenced AND materiality==high",
            },
            {
                "source_type": "story_candidate",
                "max_items": 5,
                "promoted_only": True,
                "filter": "status==promoted AND tier_guess==tier_1",
            },
            {
                "source_type": "theme_record",
                "max_items": 5,
                "promoted_only": True,
                "filter": "status==promoted",
            },
            {
                "source_type": "objection_prediction",
                "max_items": 3,
                "promoted_only": False,
                "filter": "confidence in [high,medium]",
            },
        ],
        "audience_filters": {
            "technical": {},
            "policy": {},
            "executive": {},
            "public": {},
        },
        "max_total_tokens": 6000,
    },
    "default_keynote_v1": {
        "recipe_id": "default_keynote_v1",
        "recipe_version": "1.0.0",
        "description": (
            "Keynote bundle: tier-1 stories, themes, central tension."
        ),
        "sources": [
            {
                "source_type": "story_candidate",
                "max_items": 8,
                "promoted_only": True,
                "filter": "status==promoted AND tier_guess==tier_1",
            },
            {
                "source_type": "theme_record",
                "max_items": 4,
                "promoted_only": True,
                "filter": "status==promoted",
            },
            {
                "source_type": "technical_claim",
                "max_items": 5,
                "promoted_only": True,
                "filter": "status==evidenced AND materiality==high",
            },
        ],
        "audience_filters": {
            "technical": {},
            "policy": {},
            "executive": {},
            "public": {},
        },
        "max_total_tokens": 6000,
    },
}


class RetrievalRegistry:
    """Read-only lookup of built-in retrieval recipes."""

    def get_recipe(self, recipe_id: str) -> dict:
        if recipe_id not in BUILT_IN_RECIPES:
            raise KeyError(f"unknown recipe_id: {recipe_id}")
        return BUILT_IN_RECIPES[recipe_id]

    def list_recipes(self) -> list[str]:
        return sorted(BUILT_IN_RECIPES.keys())
