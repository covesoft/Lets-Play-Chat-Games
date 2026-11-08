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
    Migration(
        version=5,
        name="ordinary_warning_and_picker_rotation",
        statements=(
            "ALTER TABLE nr_sessions ADD COLUMN pacing_version TEXT NOT NULL DEFAULT 'ordinary-pacing-1'",
            "ALTER TABLE nr_rounds ADD COLUMN ordinary_posts INTEGER NOT NULL DEFAULT 0 CHECK(ordinary_posts>=0)",
            """UPDATE nr_rounds AS r SET ordinary_posts=COALESCE((SELECT SUM(s.ordinary_posts)
                FROM nr_round_scores AS s WHERE s.session_id=r.session_id AND s.round_id=r.round_id),0)""",
            """CREATE TABLE nr_round_pacing (
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                ruleset_version INTEGER NOT NULL CHECK(ruleset_version>=0),
                owner_revision INTEGER NOT NULL CHECK(owner_revision>=0),
                started_at_utc INTEGER NOT NULL,
                estimated_players INTEGER NOT NULL CHECK(estimated_players>=0),
                message_target INTEGER NOT NULL CHECK(message_target>=15),
                random_seed INTEGER NOT NULL CHECK(random_seed>=0),
                target_reached_at_utc INTEGER,
                warning_published_at_utc INTEGER,
                selection_due_at_utc INTEGER,
                picker_id INTEGER CHECK(picker_id>0),
                picker_cycle INTEGER CHECK(picker_cycle>=0),
                stage TEXT NOT NULL CHECK(stage IN
                    ('waiting','warning_queued','warning_published','selected','revival','completed','cancelled')),
                PRIMARY KEY(session_id,round_id),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_rounds ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE nr_pacing_outputs (
                output_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('warning','revival','recovery')),
                message_id INTEGER CHECK(message_id>0),
                acknowledged_at_utc INTEGER,
                retired_at_utc INTEGER,
                UNIQUE(session_id,round_id,kind),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_round_pacing ON DELETE CASCADE
            ) STRICT""",
            """CREATE INDEX nr_pending_pacing_outputs ON nr_pacing_outputs(output_id)
                WHERE acknowledged_at_utc IS NULL AND retired_at_utc IS NULL""",
            """CREATE TABLE nr_rotation (
                session_id TEXT PRIMARY KEY REFERENCES nr_sessions ON DELETE CASCADE,
                cycle INTEGER NOT NULL DEFAULT 0 CHECK(cycle>=0),
                previous_picker INTEGER CHECK(previous_picker>0)
            ) STRICT""",
            """CREATE TABLE nr_rotation_members (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                actor_id INTEGER NOT NULL CHECK(actor_id>0),
                served_cycle INTEGER NOT NULL DEFAULT -1 CHECK(served_cycle>=-1),
                PRIMARY KEY(session_id,actor_id)
            ) STRICT""",
            """CREATE TABLE nr_picker_candidates (
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL CHECK(actor_id>0),
                ordinary_posts INTEGER NOT NULL CHECK(ordinary_posts>=5),
                actual_n TEXT,
                actual_d TEXT,
                raw_faults INTEGER NOT NULL CHECK(raw_faults>=0),
                excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0,1)),
                CHECK((actual_n IS NULL)=(actual_d IS NULL)),
                PRIMARY KEY(session_id,round_id,actor_id),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_round_pacing ON DELETE CASCADE
            ) STRICT""",
        ),
    ),
    Migration(
        version=6,
        name="guild_admission_and_opening_activation",
        statements=(
            """CREATE TABLE nr_guild_entitlements (
                guild_id INTEGER PRIMARY KEY CHECK(guild_id>0),
                premium_until_utc INTEGER
            ) STRICT""",
            """CREATE TABLE nr_admissions (
                session_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL CHECK(guild_id>0),
                channel_id INTEGER NOT NULL CHECK(channel_id>0),
                starter_id INTEGER CHECK(starter_id>0),
                create_source_id TEXT,
                version TEXT NOT NULL DEFAULT 'opening-1',
                revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
                stage TEXT NOT NULL CHECK(stage IN
                    ('lobby_prepared','lobby_published','opening_prepared','opening_published',
                    'selected','recovery','active','legacy','ended')),
                playing_session_id TEXT UNIQUE REFERENCES nr_sessions ON DELETE CASCADE,
                phrase_draw_limit INTEGER CHECK(phrase_draw_limit IN (5,15)),
                random_seed INTEGER CHECK(random_seed>=0),
                config_json TEXT,
                prepared_at_utc INTEGER,
                lobby_published_at_utc INTEGER,
                lobby_deadline_utc INTEGER,
                picker_id INTEGER CHECK(picker_id>0),
                hand_json TEXT,
                offer_prepared_at_utc INTEGER,
                offer_published_at_utc INTEGER,
                selected_at_utc INTEGER,
                selected_incoming TEXT,
                chosen_by_picker INTEGER NOT NULL DEFAULT 0 CHECK(chosen_by_picker IN (0,1)),
                effective_at_utc INTEGER,
                announced_at_utc INTEGER,
                end_reason TEXT CHECK(end_reason IN ('expired','manager','unavailable')),
                last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence>=0),
                observed_at_utc INTEGER
            ) STRICT""",
            """CREATE UNIQUE INDEX nr_admitted_channel ON nr_admissions(channel_id)
                WHERE stage<>'ended'""",
            "CREATE INDEX nr_guild_slots ON nr_admissions(guild_id,stage)",
            """CREATE UNIQUE INDEX nr_admission_request ON nr_admissions(guild_id,create_source_id)
                WHERE create_source_id IS NOT NULL""",
            """INSERT INTO nr_admissions(session_id,guild_id,channel_id,stage,playing_session_id)
                SELECT session_id,guild_id,channel_id,
                    CASE WHEN lifecycle='ended' THEN 'ended' ELSE 'legacy' END,session_id
                FROM nr_sessions""",
            """CREATE TABLE nr_lobby_members (
                session_id TEXT NOT NULL REFERENCES nr_admissions ON DELETE CASCADE,
                actor_id INTEGER NOT NULL CHECK(actor_id>0),
                PRIMARY KEY(session_id,actor_id)
            ) STRICT""",
            """CREATE TABLE nr_opening_outputs (
                output_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES nr_admissions ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('lobby','offer','activation','recovery','closed')),
                message_id INTEGER CHECK(message_id>0),
                acknowledged_at_utc INTEGER,
                retired_at_utc INTEGER,
                UNIQUE(session_id,kind)
            ) STRICT""",
            """CREATE INDEX nr_pending_opening_outputs ON nr_opening_outputs(output_id)
                WHERE acknowledged_at_utc IS NULL AND retired_at_utc IS NULL""",
            """CREATE TABLE nr_preplay_sources (
                session_id TEXT NOT NULL REFERENCES nr_admissions ON DELETE CASCADE,
                event_kind TEXT NOT NULL CHECK(event_kind IN ('message_create','reaction_add')),
                source_id TEXT NOT NULL,
                PRIMARY KEY(session_id,event_kind,source_id)
            ) STRICT""",
        ),
    ),
    Migration(
        version=7,
        name="ordinary_attempts_held_and_revival",
        statements=(
            "ALTER TABLE nr_round_pacing ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt>0)",
            "ALTER TABLE nr_round_pacing ADD COLUMN next_estimate INTEGER CHECK(next_estimate>=0)",
            "ALTER TABLE nr_round_pacing ADD COLUMN revival_id TEXT",
            "UPDATE nr_sessions SET pacing_version='ordinary-pacing-2' WHERE pacing_version='ordinary-pacing-1'",
            "CREATE TEMP TABLE m7_sequences AS SELECT name,seq FROM sqlite_sequence WHERE name='nr_pacing_outputs'",
            "DROP INDEX nr_pending_pacing_outputs",
            "ALTER TABLE nr_pacing_outputs RENAME TO nr_pacing_outputs_v5",
            """CREATE TABLE nr_pacing_outputs (
                output_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,round_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('warning','revival','recovery')),
                message_id INTEGER CHECK(message_id>0),acknowledged_at_utc INTEGER,retired_at_utc INTEGER,
                attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt>0),
                UNIQUE(session_id,round_id,attempt,kind),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_round_pacing ON DELETE CASCADE
            ) STRICT""",
            """INSERT INTO nr_pacing_outputs(output_id,session_id,round_id,kind,message_id,acknowledged_at_utc,retired_at_utc)
                SELECT output_id,session_id,round_id,kind,message_id,acknowledged_at_utc,retired_at_utc FROM nr_pacing_outputs_v5""",
            "UPDATE sqlite_sequence SET seq=MAX(seq,COALESCE((SELECT seq FROM m7_sequences),0)) WHERE name='nr_pacing_outputs'",
            "INSERT INTO sqlite_sequence(name,seq) SELECT name,seq FROM m7_sequences WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name='nr_pacing_outputs')",
            "DROP TABLE nr_pacing_outputs_v5",
            "DROP TABLE m7_sequences",
            """CREATE INDEX nr_pending_pacing_outputs ON nr_pacing_outputs(output_id)
                WHERE acknowledged_at_utc IS NULL AND retired_at_utc IS NULL""",
            "ALTER TABLE nr_picker_candidates RENAME TO nr_picker_candidates_v5",
            """CREATE TABLE nr_picker_candidates (
                session_id TEXT NOT NULL,round_id TEXT NOT NULL,actor_id INTEGER NOT NULL CHECK(actor_id>0),
                ordinary_posts INTEGER NOT NULL CHECK(ordinary_posts>=5),actual_n TEXT,actual_d TEXT,
                raw_faults INTEGER NOT NULL CHECK(raw_faults>=0),excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0,1)),
                attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt>0),
                CHECK((actual_n IS NULL)=(actual_d IS NULL)),
                PRIMARY KEY(session_id,round_id,attempt,actor_id),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_round_pacing ON DELETE CASCADE
            ) STRICT""",
            """INSERT INTO nr_picker_candidates(session_id,round_id,actor_id,ordinary_posts,actual_n,actual_d,raw_faults,excluded)
                SELECT session_id,round_id,actor_id,ordinary_posts,actual_n,actual_d,raw_faults,excluded FROM nr_picker_candidates_v5""",
            "DROP TABLE nr_picker_candidates_v5",
            """CREATE TABLE nr_pacing_history (
                session_id TEXT NOT NULL,round_id TEXT NOT NULL,attempt INTEGER NOT NULL CHECK(attempt>0),
                snapshot_json TEXT NOT NULL,retired_at_utc INTEGER NOT NULL,
                PRIMARY KEY(session_id,round_id,attempt),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_rounds ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE nr_session_activity (
                session_id TEXT PRIMARY KEY REFERENCES nr_sessions ON DELETE CASCADE,
                last_human_at_utc INTEGER NOT NULL
            ) STRICT""",
            """CREATE TABLE nr_paused_sources (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                event_kind TEXT NOT NULL CHECK(event_kind IN ('message_create','reaction_add')),
                source_id TEXT NOT NULL,PRIMARY KEY(session_id,event_kind,source_id)
            ) STRICT""",
            """CREATE TABLE nr_evolution (
                session_id TEXT PRIMARY KEY REFERENCES nr_sessions ON DELETE CASCADE,round_id TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT 'ordinary-evolution-1',config_json TEXT NOT NULL,
                phrase_draw_limit INTEGER CHECK(phrase_draw_limit IN (5,15)),
                phase TEXT NOT NULL CHECK(phase IN ('normal','held','restoring','choosing','revival','ended')),
                idle_anchor_at_utc INTEGER,held_token TEXT,
                FOREIGN KEY(session_id,round_id) REFERENCES nr_rounds ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE nr_revivals (
                session_id TEXT NOT NULL,revival_id TEXT NOT NULL,round_id TEXT NOT NULL,
                attempt INTEGER NOT NULL CHECK(attempt>0),owner_revision INTEGER NOT NULL,
                prepared_at_utc INTEGER NOT NULL,published_at_utc INTEGER,deadline_utc INTEGER,
                random_seed INTEGER NOT NULL CHECK(random_seed>=0),picker_id INTEGER CHECK(picker_id>0),
                stage TEXT NOT NULL CHECK(stage IN ('prepared','published','quorum','expired','cancelled')),
                PRIMARY KEY(session_id,revival_id),UNIQUE(session_id,round_id,attempt),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_rounds ON DELETE CASCADE
            ) STRICT""",
            "CREATE UNIQUE INDEX nr_one_revival ON nr_revivals(session_id) WHERE stage IN ('prepared','published')",
            """CREATE TABLE nr_revival_members (
                session_id TEXT NOT NULL,revival_id TEXT NOT NULL,actor_id INTEGER NOT NULL CHECK(actor_id>0),
                PRIMARY KEY(session_id,revival_id,actor_id),
                FOREIGN KEY(session_id,revival_id) REFERENCES nr_revivals ON DELETE CASCADE
            ) STRICT""",
            """CREATE TABLE nr_evolution_outputs (
                output_id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                token TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('held','closed_silent')),
                message_id INTEGER CHECK(message_id>0),acknowledged_at_utc INTEGER,retired_at_utc INTEGER,
                UNIQUE(session_id,token,kind)
            ) STRICT""",
            "CREATE INDEX nr_pending_evolution_outputs ON nr_evolution_outputs(output_id) WHERE acknowledged_at_utc IS NULL AND retired_at_utc IS NULL",
            """CREATE TABLE nr_offer_refreshes (
                session_id TEXT NOT NULL,old_offer_id TEXT NOT NULL,new_offer_id TEXT NOT NULL,
                PRIMARY KEY(session_id,old_offer_id),UNIQUE(session_id,new_offer_id),
                FOREIGN KEY(session_id,old_offer_id) REFERENCES nr_offers ON DELETE CASCADE,
                FOREIGN KEY(session_id,new_offer_id) REFERENCES nr_offers ON DELETE CASCADE
            ) STRICT""",
        ),
    ),
    Migration(
        version=8,
        name="explicit_startup_recovery_barrier",
        statements=(
            """CREATE TABLE nr_recoveries (
                session_id TEXT NOT NULL REFERENCES nr_admissions ON DELETE CASCADE,
                recovery_id TEXT NOT NULL,version TEXT NOT NULL DEFAULT 'startup-recovery-1',
                stage TEXT NOT NULL CHECK(stage IN ('paused','ready','reading','resumed','closed','superseded')),
                original_stage TEXT NOT NULL,original_lifecycle TEXT,original_pause TEXT,
                original_round TEXT,owner_revision INTEGER NOT NULL,
                began_at_utc INTEGER NOT NULL,plan TEXT CHECK(plan IN ('continue','hold')),
                templates_json TEXT,notice_message_id INTEGER CHECK(notice_message_id>0),
                notice_published_at_utc INTEGER,resume_at_utc INTEGER,notice_prepared_at_utc INTEGER,resumed_at_utc INTEGER,
                PRIMARY KEY(session_id,recovery_id)
            ) STRICT""",
            "CREATE UNIQUE INDEX nr_one_recovery ON nr_recoveries(session_id) WHERE stage IN ('paused','ready','reading')",
            "CREATE INDEX nr_recovery_resume_boundary ON nr_recoveries(session_id,resumed_at_utc DESC) WHERE resumed_at_utc IS NOT NULL",
            """CREATE TABLE nr_recovery_work (
                session_id TEXT NOT NULL,recovery_id TEXT NOT NULL,
                family TEXT NOT NULL CHECK(family IN ('opening','ordinary','pacing','evolution')),
                output_id INTEGER NOT NULL CHECK(output_id>0),kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown' CHECK(status IN ('unknown','unsent','found')),
                message_id INTEGER CHECK(message_id>0),published_at_utc INTEGER,
                PRIMARY KEY(session_id,recovery_id,family,output_id),
                FOREIGN KEY(session_id,recovery_id) REFERENCES nr_recoveries ON DELETE CASCADE
            ) STRICT""",
        ),
    ),
    Migration(
        version=9,
        name="discord_opening_delivery_attempts",
        statements=(
            """CREATE TABLE nr_discord_deliveries (
                output_id INTEGER PRIMARY KEY REFERENCES nr_opening_outputs ON DELETE CASCADE,
                state TEXT NOT NULL CHECK(state IN ('sending','delivered','failed')),
                attempted_at_utc INTEGER NOT NULL,
                message_id INTEGER CHECK(message_id>0)
            ) STRICT""",
        ),
    ),
    Migration(
        version=10,
        name="discord_multi_family_delivery_guards",
        statements=(
            """ALTER TABLE nr_discord_deliveries RENAME TO nr_discord_deliveries_v9""",
            """CREATE TABLE nr_discord_deliveries (
                session_id TEXT NOT NULL REFERENCES nr_admissions ON DELETE CASCADE,
                family TEXT NOT NULL CHECK(family IN ('opening','ordinary','pacing','evolution')),
                output_id INTEGER NOT NULL CHECK(output_id>0),
                state TEXT NOT NULL CHECK(state IN ('sending','delivered','failed')),
                attempted_at_utc INTEGER NOT NULL,
                message_id INTEGER CHECK(message_id>0),ack_sequence INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(family,output_id)
            ) STRICT""",
            """INSERT INTO nr_discord_deliveries
                SELECT o.session_id,'opening',d.output_id,d.state,d.attempted_at_utc,d.message_id,0
                FROM nr_discord_deliveries_v9 d JOIN nr_opening_outputs o USING(output_id)""",
            "DROP TABLE nr_discord_deliveries_v9",
            "CREATE INDEX nr_discord_known_panels ON nr_discord_deliveries(session_id,ack_sequence DESC,attempted_at_utc DESC) WHERE message_id IS NOT NULL",
        ),
    ),
)
SCHEMA_VERSION = MIGRATIONS[-1].version
