-- Populated released v0.2.7/schema6 from its installed wheel; synthetic gameplay only.
BEGIN TRANSACTION;
CREATE TABLE nr_activations (
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
            ) STRICT;
INSERT INTO "nr_activations" VALUES('smoke','smoke:opening',2,'smoke:opening:round',1794009635000000,1794009631000000,'activated');
CREATE TABLE nr_admissions (
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
            ) STRICT;
INSERT INTO "nr_admissions" VALUES('smoke',101,202,303,'1','opening-1',3,'active','smoke',5,7106521602475165645,'{"format":1,"parser":"discord-text-1","evaluator":"ordinary-subset-2","templates":["NR004","NR006","NR034"],"phrases":[]}',1794009600000000,1794009605000000,1794009785000000,303,'{"format":1,"offer":"smoke:opening","picker":303,"owner":[101,202,"smoke","opening_choice","ordinary","choosing",null,0,"smoke:opening","smoke:opening:activation",null,2],"active":[],"deadline":["opening-choice",1794009680000000],"options":[[["smoke:opening:NR006","NR006",[]],[null]],[["smoke:opening:NR034","NR034",[]],[null]],[["smoke:opening:NR004","NR004",[]],[null]]],"evaluator":"ordinary-subset-2","parser":"discord-text-1"}',1794009611000000,1794009620000000,1794009630000000,'smoke:opening:NR034',1,1794009635000000,1794009631000000,NULL,9,1794009635000000);
CREATE TABLE nr_badges (
                actor_id INTEGER NOT NULL REFERENCES nr_profiles ON DELETE CASCADE,
                badge_key TEXT NOT NULL CHECK(badge_key IN
                    ('rulemaker','spotless','familiar_face','party_animal','very_careful')),
                PRIMARY KEY(actor_id,badge_key)
            ) STRICT;
INSERT INTO "nr_badges" VALUES(303,'rulemaker');
CREATE TABLE nr_fault_counts (
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
            ) STRICT;
CREATE TABLE nr_flow_watermarks (
                session_id TEXT PRIMARY KEY REFERENCES nr_sessions ON DELETE CASCADE,
                last_sequence INTEGER NOT NULL CHECK(last_sequence > 0),
                observed_at_utc INTEGER NOT NULL
            ) STRICT;
INSERT INTO "nr_flow_watermarks" VALUES('smoke',14,1794009640000000);
CREATE TABLE nr_game_finalizations (
                session_id TEXT PRIMARY KEY REFERENCES nr_sessions ON DELETE CASCADE,
                final_round_id TEXT NOT NULL,
                FOREIGN KEY(session_id,final_round_id) REFERENCES nr_round_resolutions
                    ON DELETE CASCADE
            ) STRICT;
CREATE TABLE nr_guild_entitlements (
                guild_id INTEGER PRIMARY KEY CHECK(guild_id>0),
                premium_until_utc INTEGER
            ) STRICT;
CREATE TABLE nr_lobby_members (
                session_id TEXT NOT NULL REFERENCES nr_admissions ON DELETE CASCADE,
                actor_id INTEGER NOT NULL CHECK(actor_id>0),
                PRIMARY KEY(session_id,actor_id)
            ) STRICT;
INSERT INTO "nr_lobby_members" VALUES('smoke',303);
INSERT INTO "nr_lobby_members" VALUES('smoke',404);
INSERT INTO "nr_lobby_members" VALUES('smoke',505);
CREATE TABLE nr_marker_intents (
                intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_kind TEXT NOT NULL DEFAULT 'message_create'
                    CHECK(event_kind = 'message_create'),
                source_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL CHECK(actor_id > 0),
                UNIQUE(session_id, event_kind, source_id),
                FOREIGN KEY(session_id, event_kind, source_id)
                    REFERENCES nr_processed_events ON DELETE CASCADE
            ) STRICT;
CREATE TABLE nr_offers (
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
            ) STRICT;
INSERT INTO "nr_offers" VALUES('smoke','smoke:opening','{"format":1,"offer":"smoke:opening","picker":303,"owner":[101,202,"smoke","opening_choice","ordinary","choosing",null,0,"smoke:opening","smoke:opening:activation",null,2],"active":[],"deadline":["opening-choice",1794009680000000],"options":[[["smoke:opening:NR006","NR006",[]],[null]],[["smoke:opening:NR034","NR034",[]],[null]],[["smoke:opening:NR004","NR004",[]],[null]]],"evaluator":"ordinary-subset-2","parser":"discord-text-1"}',1794009611000000,1794009620000000,1794009630000000,'smoke:opening:NR034',NULL,1,0,'activated');
CREATE TABLE nr_opening_outputs (
                output_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES nr_admissions ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('lobby','offer','activation','recovery','closed')),
                message_id INTEGER CHECK(message_id>0),
                acknowledged_at_utc INTEGER,
                retired_at_utc INTEGER,
                UNIQUE(session_id,kind)
            ) STRICT;
INSERT INTO "nr_opening_outputs" VALUES(1,'smoke','lobby',1001,1794009605000000,NULL);
INSERT INTO "nr_opening_outputs" VALUES(2,'smoke','offer',1002,1794009620000000,NULL);
INSERT INTO "nr_opening_outputs" VALUES(3,'smoke','activation',1003,1794009631000000,NULL);
CREATE TABLE nr_ordinary_outputs (
                output_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('offer','activation','recovery')),
                message_id INTEGER CHECK(message_id > 0),
                acknowledged_at_utc INTEGER,
                retired_at_utc INTEGER,
                UNIQUE(session_id,offer_id,kind),
                FOREIGN KEY(session_id,offer_id) REFERENCES nr_offers ON DELETE CASCADE
            ) STRICT;
INSERT INTO "nr_ordinary_outputs" VALUES(1,'smoke','smoke:opening','activation',1003,1794009631000000,NULL);
INSERT INTO "nr_ordinary_outputs" VALUES(2,'smoke','smoke:opening','offer',1002,1794009620000000,NULL);
CREATE TABLE nr_pacing_outputs (
                output_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('warning','revival','recovery')),
                message_id INTEGER CHECK(message_id>0),
                acknowledged_at_utc INTEGER,
                retired_at_utc INTEGER,
                UNIQUE(session_id,round_id,kind),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_round_pacing ON DELETE CASCADE
            ) STRICT;
CREATE TABLE nr_participants (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                actor_id INTEGER NOT NULL CHECK(actor_id > 0),

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
,
                PRIMARY KEY(session_id, actor_id)
            ) STRICT;
INSERT INTO "nr_participants" VALUES('smoke',303,0,0,0,0,'0','1',0,0,0,'0','1',0);
INSERT INTO "nr_participants" VALUES('smoke',404,0,0,0,0,'0','1',0,0,0,'0','1',0);
INSERT INTO "nr_participants" VALUES('smoke',505,0,0,0,0,'0','1',0,0,0,'0','1',0);
INSERT INTO "nr_participants" VALUES('smoke',606,5,0,5,5,'5','1',0,0,0,'0','1',0);
CREATE TABLE nr_picker_candidates (
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
            ) STRICT;
CREATE TABLE nr_player_progress (
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
            ) STRICT;
INSERT INTO "nr_player_progress" VALUES('smoke',303,1,0,0,0,0,0);
INSERT INTO "nr_player_progress" VALUES('smoke',404,1,0,0,0,0,0);
INSERT INTO "nr_player_progress" VALUES('smoke',505,1,0,0,0,0,0);
INSERT INTO "nr_player_progress" VALUES('smoke',606,1,1,0,0,0,0);
CREATE TABLE nr_preplay_sources (
                session_id TEXT NOT NULL REFERENCES nr_admissions ON DELETE CASCADE,
                event_kind TEXT NOT NULL CHECK(event_kind IN ('message_create','reaction_add')),
                source_id TEXT NOT NULL,
                PRIMARY KEY(session_id,event_kind,source_id)
            ) STRICT;
INSERT INTO "nr_preplay_sources" VALUES('smoke','message_create','before');
CREATE TABLE nr_processed_events (
                session_id TEXT NOT NULL,
                event_kind TEXT NOT NULL CHECK(event_kind IN ('message_create','reaction_add')),
                source_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                ruleset_version INTEGER NOT NULL CHECK(ruleset_version >= 0),
                owner_revision INTEGER NOT NULL CHECK(owner_revision >= 0),
                PRIMARY KEY(session_id, event_kind, source_id),
                FOREIGN KEY(session_id, round_id) REFERENCES nr_rounds ON DELETE CASCADE
            ) STRICT;
INSERT INTO "nr_processed_events" VALUES('smoke','message_create','boundary','smoke:opening:round',1,0);
INSERT INTO "nr_processed_events" VALUES('smoke','message_create','11','smoke:opening:round',1,0);
INSERT INTO "nr_processed_events" VALUES('smoke','message_create','12','smoke:opening:round',1,0);
INSERT INTO "nr_processed_events" VALUES('smoke','message_create','13','smoke:opening:round',1,0);
INSERT INTO "nr_processed_events" VALUES('smoke','message_create','14','smoke:opening:round',1,0);
CREATE TABLE nr_profiles (
                actor_id INTEGER PRIMARY KEY CHECK(actor_id > 0),
                games_played INTEGER NOT NULL DEFAULT 0 CHECK(games_played >= 0)
            ) STRICT;
INSERT INTO "nr_profiles" VALUES(303,0);
INSERT INTO "nr_profiles" VALUES(606,1);
CREATE TABLE nr_reaction_offenses (
                session_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL CHECK(actor_id > 0),
                target_message_id INTEGER NOT NULL CHECK(target_message_id > 0),
                round_id TEXT NOT NULL,
                PRIMARY KEY(session_id, actor_id, target_message_id),
                FOREIGN KEY(session_id, round_id) REFERENCES nr_rounds ON DELETE CASCADE,
                FOREIGN KEY(session_id, actor_id) REFERENCES nr_participants ON DELETE CASCADE
            ) STRICT;
CREATE TABLE nr_rotation (
                session_id TEXT PRIMARY KEY REFERENCES nr_sessions ON DELETE CASCADE,
                cycle INTEGER NOT NULL DEFAULT 0 CHECK(cycle>=0),
                previous_picker INTEGER CHECK(previous_picker>0)
            ) STRICT;
INSERT INTO "nr_rotation" VALUES('smoke',0,303);
CREATE TABLE nr_rotation_members (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                actor_id INTEGER NOT NULL CHECK(actor_id>0),
                served_cycle INTEGER NOT NULL DEFAULT -1 CHECK(served_cycle>=-1),
                PRIMARY KEY(session_id,actor_id)
            ) STRICT;
INSERT INTO "nr_rotation_members" VALUES('smoke',303,0);
CREATE TABLE nr_round_closures (
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                closed_at_utc INTEGER NOT NULL,
                PRIMARY KEY(session_id,round_id),
                UNIQUE(session_id,offer_id),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_rounds ON DELETE CASCADE,
                FOREIGN KEY(session_id,offer_id) REFERENCES nr_offers ON DELETE CASCADE
            ) STRICT;
CREATE TABLE nr_round_pacing (
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
            ) STRICT;
INSERT INTO "nr_round_pacing" VALUES('smoke','smoke:opening:round',1,0,1794009635000000,3,15,7106521602475165645,NULL,NULL,NULL,NULL,NULL,'waiting');
CREATE TABLE nr_round_resolutions (
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                round_number INTEGER NOT NULL CHECK(round_number > 0),
                kind TEXT NOT NULL CHECK(kind IN ('activation','ending')),
                PRIMARY KEY(session_id,round_id),
                UNIQUE(session_id,round_number),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_rounds ON DELETE CASCADE
            ) STRICT;
CREATE TABLE nr_round_scores (
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,

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
,
                PRIMARY KEY(session_id, round_id, actor_id),
                FOREIGN KEY(session_id, round_id) REFERENCES nr_rounds ON DELETE CASCADE,
                FOREIGN KEY(session_id, actor_id) REFERENCES nr_participants ON DELETE CASCADE
            ) STRICT;
INSERT INTO "nr_round_scores" VALUES('smoke','smoke:opening:round',303,0,0,0,0,'0','1',0,0,0,'0','1',0);
INSERT INTO "nr_round_scores" VALUES('smoke','smoke:opening:round',404,0,0,0,0,'0','1',0,0,0,'0','1',0);
INSERT INTO "nr_round_scores" VALUES('smoke','smoke:opening:round',505,0,0,0,0,'0','1',0,0,0,'0','1',0);
INSERT INTO "nr_round_scores" VALUES('smoke','smoke:opening:round',606,5,0,5,5,'5','1',0,0,0,'0','1',0);
CREATE TABLE nr_rounds (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                round_id TEXT NOT NULL,
                human_messages INTEGER NOT NULL DEFAULT 0 CHECK(human_messages >= 0),
                whispers INTEGER NOT NULL DEFAULT 0 CHECK(whispers >= 0 AND whispers <= human_messages), started_at_utc INTEGER, round_number INTEGER NOT NULL DEFAULT 1 CHECK(round_number > 0), ordinary_posts INTEGER NOT NULL DEFAULT 0 CHECK(ordinary_posts>=0),
                PRIMARY KEY(session_id, round_id)
            ) STRICT;
INSERT INTO "nr_rounds" VALUES('smoke','smoke:opening:round',5,0,1794009635000000,1,5);
CREATE TABLE nr_sessions (
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
                whispers INTEGER NOT NULL DEFAULT 0 CHECK(whispers >= 0 AND whispers <= human_messages), progression_version TEXT NOT NULL DEFAULT 'general-1', pacing_version TEXT NOT NULL DEFAULT 'ordinary-pacing-1',
                CHECK((lifecycle = 'paused') = (pause_reason IS NOT NULL))
            ) STRICT;
INSERT INTO "nr_sessions" VALUES('smoke',101,202,'playing','ordinary','idle','smoke:opening:round',1,0,NULL,NULL,NULL,'{"rules":[["smoke:opening:NR034","NR034",[]]],"exclusions":[]}','discord-text-1','ordinary-subset-2','normalized-1',5,0,'general-1','ordinary-pacing-1');
CREATE TABLE nr_streak_notices (
                notice_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                milestone INTEGER NOT NULL CHECK(milestone IN (3,5,7,9)),
                acknowledged INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged IN (0,1)),
                UNIQUE(session_id,round_id,actor_id),
                FOREIGN KEY(session_id,round_id) REFERENCES nr_round_resolutions ON DELETE CASCADE,
                FOREIGN KEY(session_id,actor_id) REFERENCES nr_player_progress ON DELETE CASCADE
            ) STRICT;
CREATE TABLE nr_used_phrases (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                phrase_key TEXT NOT NULL,
                PRIMARY KEY(session_id,phrase_key)
            ) STRICT;
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL);
INSERT INTO "schema_migrations" VALUES(1,'foundation');
INSERT INTO "schema_migrations" VALUES(2,'new_rules_event_unit_of_work');
INSERT INTO "schema_migrations" VALUES(3,'ordinary_offer_publication_and_cutover');
INSERT INTO "schema_migrations" VALUES(4,'new_rules_general_progression');
INSERT INTO "schema_migrations" VALUES(5,'ordinary_warning_and_picker_rotation');
INSERT INTO "schema_migrations" VALUES(6,'guild_admission_and_opening_activation');
CREATE UNIQUE INDEX nr_live_channel ON nr_sessions(channel_id)
                WHERE lifecycle <> 'ended';
CREATE UNIQUE INDEX nr_one_open_offer ON nr_offers(session_id)
                WHERE stage IN ('prepared','published','selected');
CREATE INDEX nr_pending_activation ON nr_activations(session_id,effective_at_utc)
                WHERE stage IN ('prepared','announced');
CREATE INDEX nr_pending_ordinary_output ON nr_ordinary_outputs(output_id)
                WHERE acknowledged_at_utc IS NULL AND retired_at_utc IS NULL;
CREATE UNIQUE INDEX nr_round_number ON nr_rounds(session_id,round_number);
CREATE INDEX nr_score_actor_round ON nr_round_scores(session_id,actor_id,round_id);
CREATE INDEX nr_pending_streak_notices ON nr_streak_notices(notice_id)
                WHERE acknowledged=0;
CREATE INDEX nr_pending_pacing_outputs ON nr_pacing_outputs(output_id)
                WHERE acknowledged_at_utc IS NULL AND retired_at_utc IS NULL;
CREATE UNIQUE INDEX nr_admitted_channel ON nr_admissions(channel_id)
                WHERE stage<>'ended';
CREATE INDEX nr_guild_slots ON nr_admissions(guild_id,stage);
CREATE UNIQUE INDEX nr_admission_request ON nr_admissions(guild_id,create_source_id)
                WHERE create_source_id IS NOT NULL;
CREATE INDEX nr_pending_opening_outputs ON nr_opening_outputs(output_id)
                WHERE acknowledged_at_utc IS NULL AND retired_at_utc IS NULL;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('nr_opening_outputs',3);
INSERT INTO "sqlite_sequence" VALUES('nr_ordinary_outputs',2);
COMMIT;
PRAGMA application_id=1280330841;
PRAGMA user_version=6;
