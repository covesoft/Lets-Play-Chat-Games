-- Populated schema 3 produced by installed v0.2.4 wheel; synthetic IDs/scores only.
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
INSERT INTO "nr_activations" VALUES('session','offer-one',2,'offer-one:round',1794009607000000,1794009603000000,'activated');
INSERT INTO "nr_activations" VALUES('session','a-second-offer',5,'a-second-offer:round',1794009627000000,1794009623000000,'activated');
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
INSERT INTO "nr_flow_watermarks" VALUES('session',20,1794009627000000);
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
INSERT INTO "nr_offers" VALUES('session','offer-one','{"format":1,"offer":"offer-one","picker":303,"owner":[101,202,"session","playing","ordinary","choosing","z-first",1,"offer-one","offer-one:activation",null,2],"active":[["echo","NR022",[]],["sorry","NR025",[]]],"deadline":["offer-one",1794009661000000],"options":[[["offer-one:NR006","NR006",[]],[null]],[["offer-one:NR027","NR027",[]],["echo","sorry"]],[["offer-one:NR004","NR004",[]],[null]]],"evaluator":"ordinary-subset-2","parser":"discord-text-1"}',1794009600000000,1794009601000000,1794009602000000,'offer-one:NR027','echo',1,0,'activated');
INSERT INTO "nr_offers" VALUES('session','a-second-offer','{"format":1,"offer":"a-second-offer","picker":303,"owner":[101,202,"session","playing","ordinary","choosing","offer-one:round",2,"a-second-offer","a-second-offer:activation",null,5],"active":[["sorry","NR025",[]],["offer-one:NR027","NR027",[]]],"deadline":["a-second-offer",1794009681000000],"options":[[["a-second-offer:NR006","NR006",[]],[null]],[["a-second-offer:NR022","NR022",[]],["sorry","offer-one:NR027"]],[["a-second-offer:NR004","NR004",[]],[null]]],"evaluator":"ordinary-subset-2","parser":"discord-text-1"}',1794009620000000,1794009621000000,1794009622000000,'a-second-offer:NR022','sorry',1,0,'activated');
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
INSERT INTO "nr_ordinary_outputs" VALUES(1,'session','offer-one','offer',1001,1794009601000000,NULL);
INSERT INTO "nr_ordinary_outputs" VALUES(2,'session','offer-one','activation',1002,1794009603000000,NULL);
INSERT INTO "nr_ordinary_outputs" VALUES(3,'session','a-second-offer','offer',1003,1794009621000000,NULL);
INSERT INTO "nr_ordinary_outputs" VALUES(4,'session','a-second-offer','activation',1004,1794009623000000,NULL);
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
INSERT INTO "nr_participants" VALUES('session',303,5,0,5,5,'5','1',0,0,0,'0','1',0);
INSERT INTO "nr_participants" VALUES('session',404,5,0,5,5,'5','1',0,0,0,'0','1',0);
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
INSERT INTO "nr_processed_events" VALUES('session','message_create','1','z-first',1,0);
INSERT INTO "nr_processed_events" VALUES('session','message_create','2','z-first',1,0);
INSERT INTO "nr_processed_events" VALUES('session','message_create','3','z-first',1,0);
INSERT INTO "nr_processed_events" VALUES('session','message_create','4','z-first',1,0);
INSERT INTO "nr_processed_events" VALUES('session','message_create','5','z-first',1,0);
INSERT INTO "nr_processed_events" VALUES('session','message_create','11','offer-one:round',2,3);
INSERT INTO "nr_processed_events" VALUES('session','message_create','12','offer-one:round',2,3);
INSERT INTO "nr_processed_events" VALUES('session','message_create','13','offer-one:round',2,3);
INSERT INTO "nr_processed_events" VALUES('session','message_create','14','offer-one:round',2,3);
INSERT INTO "nr_processed_events" VALUES('session','message_create','15','offer-one:round',2,3);
CREATE TABLE nr_reaction_offenses (
                session_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL CHECK(actor_id > 0),
                target_message_id INTEGER NOT NULL CHECK(target_message_id > 0),
                round_id TEXT NOT NULL,
                PRIMARY KEY(session_id, actor_id, target_message_id),
                FOREIGN KEY(session_id, round_id) REFERENCES nr_rounds ON DELETE CASCADE,
                FOREIGN KEY(session_id, actor_id) REFERENCES nr_participants ON DELETE CASCADE
            ) STRICT;
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
INSERT INTO "nr_round_closures" VALUES('session','z-first','offer-one',1794009607000000);
INSERT INTO "nr_round_closures" VALUES('session','offer-one:round','a-second-offer',1794009627000000);
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
INSERT INTO "nr_round_scores" VALUES('session','z-first',303,5,0,5,5,'5','1',0,0,0,'0','1',0);
INSERT INTO "nr_round_scores" VALUES('session','offer-one:round',404,5,0,5,5,'5','1',0,0,0,'0','1',0);
CREATE TABLE nr_rounds (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                round_id TEXT NOT NULL,
                human_messages INTEGER NOT NULL DEFAULT 0 CHECK(human_messages >= 0),
                whispers INTEGER NOT NULL DEFAULT 0 CHECK(whispers >= 0 AND whispers <= human_messages), started_at_utc INTEGER,
                PRIMARY KEY(session_id, round_id)
            ) STRICT;
INSERT INTO "nr_rounds" VALUES('session','z-first',5,0,NULL);
INSERT INTO "nr_rounds" VALUES('session','offer-one:round',5,0,1794009607000000);
INSERT INTO "nr_rounds" VALUES('session','a-second-offer:round',0,0,1794009627000000);
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
                whispers INTEGER NOT NULL DEFAULT 0 CHECK(whispers >= 0 AND whispers <= human_messages),
                CHECK((lifecycle = 'paused') = (pause_reason IS NOT NULL))
            ) STRICT;
INSERT INTO "nr_sessions" VALUES('session',101,202,'playing','ordinary','idle','a-second-offer:round',3,6,NULL,NULL,NULL,'{"rules":[["offer-one:NR027","NR027",[]],["a-second-offer:NR022","NR022",[]]],"exclusions":[]}','discord-text-1','ordinary-subset-2','normalized-1',10,0);
CREATE TABLE nr_used_phrases (
                session_id TEXT NOT NULL REFERENCES nr_sessions ON DELETE CASCADE,
                phrase_key TEXT NOT NULL,
                PRIMARY KEY(session_id,phrase_key)
            ) STRICT;
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL);
INSERT INTO "schema_migrations" VALUES(1,'foundation');
INSERT INTO "schema_migrations" VALUES(2,'new_rules_event_unit_of_work');
INSERT INTO "schema_migrations" VALUES(3,'ordinary_offer_publication_and_cutover');
CREATE UNIQUE INDEX nr_live_channel ON nr_sessions(channel_id)
                WHERE lifecycle <> 'ended';
CREATE UNIQUE INDEX nr_one_open_offer ON nr_offers(session_id)
                WHERE stage IN ('prepared','published','selected');
CREATE INDEX nr_pending_activation ON nr_activations(session_id,effective_at_utc)
                WHERE stage IN ('prepared','announced');
CREATE INDEX nr_pending_ordinary_output ON nr_ordinary_outputs(output_id)
                WHERE acknowledged_at_utc IS NULL AND retired_at_utc IS NULL;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('nr_ordinary_outputs',4);
COMMIT;
PRAGMA application_id=1280330841;
PRAGMA user_version=3;
