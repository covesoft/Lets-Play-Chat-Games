"""Ordered, immutable migrations, run inside an explicit transaction."""

from dataclasses import dataclass

APPLICATION_ID = int.from_bytes(b"LPLY", "big")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


# Frozen schema-2 column definitions. Future score fields require a NEW migration.
_SCORE_COLUMNS_V2 = """
    human_messages INTEGER NOT NULL DEFAULT 0 CHECK(human_messages >= 0),
    whispers INTEGER NOT NULL DEFAULT 0 CHECK(whispers >= 0),
    ordinary_posts INTEGER NOT NULL DEFAULT 0 CHECK(ordinary_posts >= 0),
    ordinary_observations INTEGER NOT NULL DEFAULT 0 CHECK(ordinary_observations >= 0),
    ordinary_quality_n TEXT NOT NULL DEFAULT '0',
    ordinary_quality_d TEXT NOT NULL DEFAULT '1',
    ordinary_faults INTEGER NOT NULL DEFAULT 0 CHECK(ordinary_faults >= 0),
    ordinary_offending_messages INTEGER NOT NULL DEFAULT 0 CHECK(ordinary_offending_messages >= 0),
    mode_observations INTEGER NOT NULL DEFAULT 0 CHECK(mode_observations >= 0),
    mode_quality_n TEXT NOT NULL DEFAULT '0',
    mode_quality_d TEXT NOT NULL DEFAULT '1',
    reaction_failures INTEGER NOT NULL DEFAULT 0 CHECK(reaction_failures >= 0)
"""


MIGRATIONS = (
    Migration(
        version=1,
        name="foundation",
        statements=(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL)",
        ),
    ),
    Migration(
        version=2,
        name="new_rules_event_unit_of_work",
        statements=(
            """CREATE TABLE nr_sessions (
                session_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL CHECK(guild_id > 0),
                channel_id INTEGER NOT NULL CHECK(channel_id > 0),
                lifecycle TEXT NOT NULL CHECK(lifecycle IN
                    ('lobby','opening_choice','playing','paused','ended')),
                phase TEXT NOT NULL CHECK(phase IN ('ordinary','mode')),
                advancement TEXT NOT NULL CHECK(advancement IN ('idle','warning','choosing','held')),
                round_id TEXT NOT NULL,
                ruleset_version INTEGER NOT NULL CHECK(ruleset_version >= 0),
                revision INTEGER NOT NULL CHECK(revision >= 0),
                offer_id TEXT,
                transition_id TEXT,
                pause_reason TEXT,
                context_json TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                evaluator_version TEXT NOT NULL,
                score_version TEXT NOT NULL,
                human_messages INTEGER NOT NULL DEFAULT 0 CHECK(human_messages >= 0),
                whispers INTEGER NOT NULL DEFAULT 0 CHECK(whispers >= 0 AND whispers <= human_messages),
                CHECK((lifecycle = 'paused') = (pause_reason IS NOT NULL))
            ) STRICT""",
            """CREATE UNIQUE INDEX nr_live_channel ON nr_sessions(channel_id)
                WHERE lifecycle <> 'ended'""",
            """CREATE TABLE nr_rounds (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                round_id TEXT NOT NULL,
                human_messages INTEGER NOT NULL DEFAULT 0 CHECK(human_messages >= 0),
                whispers INTEGER NOT NULL DEFAULT 0 CHECK(whispers >= 0 AND whispers <= human_messages),
                PRIMARY KEY(session_id, round_id)
            ) STRICT""",
            f"""CREATE TABLE nr_participants (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                actor_id INTEGER NOT NULL CHECK(actor_id > 0),
                {_SCORE_COLUMNS_V2},
                PRIMARY KEY(session_id, actor_id)
            ) STRICT""",
            f"""CREATE TABLE nr_round_scores (
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                {_SCORE_COLUMNS_V2},
                PRIMARY KEY(session_id, round_id, actor_id),
                FOREIGN KEY(session_id, round_id) REFERENCES nr_rounds ON DELETE CASCADE,
                FOREIGN KEY(session_id, actor_id) REFERENCES nr_participants ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE nr_processed_events (
                session_id TEXT NOT NULL,
                event_kind TEXT NOT NULL CHECK(event_kind IN ('message_create','reaction_add')),
                source_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                ruleset_version INTEGER NOT NULL CHECK(ruleset_version >= 0),
                owner_revision INTEGER NOT NULL CHECK(owner_revision >= 0),
                PRIMARY KEY(session_id, event_kind, source_id),
                FOREIGN KEY(session_id, round_id) REFERENCES nr_rounds ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE nr_reaction_offenses (
                session_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL CHECK(actor_id > 0),
                target_message_id INTEGER NOT NULL CHECK(target_message_id > 0),
                round_id TEXT NOT NULL,
                PRIMARY KEY(session_id, actor_id, target_message_id),
                FOREIGN KEY(session_id, round_id) REFERENCES nr_rounds ON DELETE CASCADE,
                FOREIGN KEY(session_id, actor_id) REFERENCES nr_participants ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE nr_marker_intents (
                intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_kind TEXT NOT NULL DEFAULT 'message_create'
                    CHECK(event_kind = 'message_create'),
                source_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL CHECK(actor_id > 0),
                UNIQUE(session_id, event_kind, source_id),
                FOREIGN KEY(session_id, event_kind, source_id)
                    REFERENCES nr_processed_events ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE nr_fault_counts (
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                ruleset_version INTEGER NOT NULL CHECK(ruleset_version >= 0),
                rule_instance_id TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                faults INTEGER NOT NULL CHECK(faults > 0),
                PRIMARY KEY(session_id, round_id, actor_id, ruleset_version,
                    rule_instance_id, reason_code),
                FOREIGN KEY(session_id, round_id, actor_id)
                    REFERENCES nr_round_scores ON DELETE CASCADE
            ) STRICT""",
        ),
    ),
    Migration(
        version=3,
        name="ordinary_offer_publication_and_cutover",
        statements=(
            "ALTER TABLE nr_rounds ADD COLUMN started_at_utc INTEGER",
            """CREATE TABLE nr_flow_watermarks (
                session_id TEXT PRIMARY KEY REFERENCES nr_sessions ON DELETE CASCADE,
                last_sequence INTEGER NOT NULL CHECK(last_sequence > 0),
                observed_at_utc INTEGER NOT NULL
            ) STRICT""",
            """CREATE TABLE nr_offers (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                offer_id TEXT NOT NULL,
                hand_json TEXT NOT NULL,
                prepared_at_utc INTEGER NOT NULL,
                published_at_utc INTEGER,
                selected_at_utc INTEGER,
                selected_incoming TEXT,
                selected_outgoing TEXT,
                chosen_by_picker INTEGER NOT NULL DEFAULT 0 CHECK(chosen_by_picker IN (0,1)),
                completed_by_timeout INTEGER NOT NULL DEFAULT 0 CHECK(completed_by_timeout IN (0,1)),
                stage TEXT NOT NULL CHECK(stage IN
                    ('prepared','published','selected','activated','recovery','cancelled')),
                PRIMARY KEY(session_id,offer_id)
            ) STRICT""",
            """CREATE UNIQUE INDEX nr_one_open_offer ON nr_offers(session_id)
                WHERE stage IN ('prepared','published','selected')""",
            """CREATE TABLE nr_activations (
                session_id TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                owner_revision INTEGER NOT NULL,
                next_round_id TEXT NOT NULL,
                effective_at_utc INTEGER NOT NULL,
                announced_at_utc INTEGER,
                stage TEXT NOT NULL CHECK(stage IN ('prepared','announced','activated','recovery')),
                PRIMARY KEY(session_id,offer_id),
                UNIQUE(session_id,next_round_id),
                FOREIGN KEY(session_id,offer_id) REFERENCES nr_offers ON DELETE CASCADE
            ) STRICT""",
            """CREATE INDEX nr_pending_activation ON nr_activations(session_id,effective_at_utc)
                WHERE stage IN ('prepared','announced')""",
            """CREATE TABLE nr_ordinary_outputs (
                output_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('offer','activation','recovery')),
                message_id INTEGER CHECK(message_id > 0),
                acknowledged_at_utc INTEGER,
                retired_at_utc INTEGER,
                UNIQUE(session_id,offer_id,kind),
                FOREIGN KEY(session_id,offer_id) REFERENCES nr_offers ON DELETE CASCADE
            ) STRICT""",
            """CREATE INDEX nr_pending_ordinary_output ON nr_ordinary_outputs(output_id)
                WHERE acknowledged_at_utc IS NULL AND retired_at_utc IS NULL""",
            """CREATE TABLE nr_used_phrases (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                phrase_key TEXT NOT NULL,
                PRIMARY KEY(session_id,phrase_key)
            ) STRICT""",
            """CREATE TABLE nr_round_closures (
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                closed_at_utc INTEGER NOT NULL,
                PRIMARY KEY(session_id,round_id),
                UNIQUE(session_id,offer_id),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_rounds ON DELETE CASCADE,
                FOREIGN KEY(session_id,offer_id) REFERENCES nr_offers ON DELETE CASCADE
            ) STRICT""",
        ),
    ),
    Migration(
        version=4,
        name="new_rules_general_progression",
        statements=(
            "ALTER TABLE nr_sessions ADD COLUMN progression_version TEXT NOT NULL DEFAULT 'general-1'",
            "ALTER TABLE nr_rounds ADD COLUMN round_number INTEGER NOT NULL DEFAULT 1 CHECK(round_number > 0)",
            # Round creation order is durable even for legacy unknown start times.
            """WITH numbered AS MATERIALIZED (
                SELECT rowid AS rid,ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY rowid) AS n
                FROM nr_rounds)
                UPDATE nr_rounds SET round_number=(SELECT n FROM numbered WHERE rid=nr_rounds.rowid)""",
            "CREATE UNIQUE INDEX nr_round_number ON nr_rounds(session_id,round_number)",
            "CREATE INDEX nr_score_actor_round ON nr_round_scores(session_id,actor_id,round_id)",
            """CREATE TABLE nr_profiles (
                actor_id INTEGER PRIMARY KEY CHECK(actor_id > 0),
                games_played INTEGER NOT NULL DEFAULT 0 CHECK(games_played >= 0)
            ) STRICT""",
            """CREATE TABLE nr_badges (
                actor_id INTEGER NOT NULL REFERENCES nr_profiles ON DELETE CASCADE,
                badge_key TEXT NOT NULL CHECK(badge_key IN
                    ('rulemaker','spotless','familiar_face','party_animal','very_careful')),
                PRIMARY KEY(actor_id,badge_key)
            ) STRICT""",
            """CREATE TABLE nr_player_progress (
                session_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                first_round_number INTEGER CHECK(first_round_number > 0),
                game_count_credited INTEGER NOT NULL DEFAULT 0 CHECK(game_count_credited IN (0,1)),
                current_clean_streak INTEGER NOT NULL DEFAULT 0 CHECK(current_clean_streak >= 0),
                best_clean_streak INTEGER NOT NULL DEFAULT 0
                    CHECK(best_clean_streak >= current_clean_streak),
                personal_rounds_played INTEGER NOT NULL DEFAULT 0 CHECK(personal_rounds_played >= 0),
                party_rounds INTEGER NOT NULL DEFAULT 0 CHECK(party_rounds >= 0),
                PRIMARY KEY(session_id,actor_id),
                FOREIGN KEY(session_id,actor_id) REFERENCES nr_participants ON DELETE CASCADE
            ) STRICT""",
            """INSERT INTO nr_player_progress(session_id,actor_id,first_round_number)
                SELECT p.session_id,p.actor_id,(SELECT MIN(r.round_number)
                    FROM nr_round_scores AS s JOIN nr_rounds AS r USING(session_id,round_id)
                    WHERE s.session_id=p.session_id AND s.actor_id=p.actor_id)
                FROM nr_participants AS p""",
            """CREATE TABLE nr_round_resolutions (
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                round_number INTEGER NOT NULL CHECK(round_number > 0),
                kind TEXT NOT NULL CHECK(kind IN ('activation','ending')),
                PRIMARY KEY(session_id,round_id),
                UNIQUE(session_id,round_number),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_rounds ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE nr_game_finalizations (
                session_id TEXT PRIMARY KEY REFERENCES nr_sessions ON DELETE CASCADE,
                final_round_id TEXT NOT NULL,
                FOREIGN KEY(session_id,final_round_id) REFERENCES nr_round_resolutions
                    ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE nr_streak_notices (
                notice_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                milestone INTEGER NOT NULL CHECK(milestone IN (3,5,7,9)),
                acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1)),
                UNIQUE(session_id,round_id,actor_id),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_round_resolutions ON DELETE CASCADE,
                FOREIGN KEY(session_id,actor_id) REFERENCES nr_player_progress ON DELETE CASCADE
            ) STRICT""",
            """CREATE INDEX nr_pending_streak_notices ON nr_streak_notices(notice_id)
                WHERE acknowledged=0""",
        ),
    ),
)
SCHEMA_VERSION = MIGRATIONS[-1].version
