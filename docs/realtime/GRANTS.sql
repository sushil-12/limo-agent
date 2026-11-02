-- Database accounts for limoagent (MariaDB 10.4 / MySQL 8).
--
--   limoagent_ro     the AI chatbot. SELECT only. Unchanged; never gets a write grant.
--   limo_rt          the realtime service at runtime. Writes only:
--                      * its own tbl_rt_* tables
--                      * 7 status columns of tbl_reservations (column-level grant)
--                      * INSERT into tbl_booking_status_history
--   limo_rt_migrate  deploy-time only (Alembic). Creates/alters tbl_rt_* tables.
--
-- Replace limoapi, <app_host> and the passwords. <app_host> is the limoagent machine
-- ('127.0.0.1' if MySQL is local to it). Run as an admin account.

-- Runtime writer ---------------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'limo_rt'@'localhost' IDENTIFIED BY '1700limo_rt';

-- Reads: ride lookups (tbl_reservations, tbl_account), Passport token checks.
GRANT SELECT ON `limoapi`.`tbl_reservations`           TO 'limo_rt'@'localhost';
GRANT SELECT ON `limoapi`.`tbl_account`                TO 'limo_rt'@'localhost';
GRANT SELECT ON `limoapi`.`users`                      TO 'limo_rt'@'localhost';
GRANT SELECT ON `limoapi`.`oauth_access_tokens`        TO 'limo_rt'@'localhost';
GRANT SELECT ON `limoapi`.`oauth_clients`              TO 'limo_rt'@'localhost';
GRANT SELECT, INSERT ON `limoapi`.`tbl_booking_status_history` TO 'limo_rt'@'localhost';

-- Status writes: these columns and nothing else in tbl_reservations.
GRANT UPDATE (`booking_status`, `status_change_flag`, `updated_at`, `current_stop_index`,
              `current_stop_status`, `total_stops`, `stop_status_updated_at`)
      ON `limoapi`.`tbl_reservations` TO 'limo_rt'@'localhost';

-- Its own tables (created by limo_rt_migrate, so run this block after the first migration).
GRANT SELECT, INSERT, UPDATE, DELETE ON `limoapi`.`tbl_rt_location_points` TO 'limo_rt'@'localhost';
GRANT SELECT, INSERT, UPDATE ON `limoapi`.`tbl_rt_chat_messages`   TO 'limo_rt'@'localhost';
GRANT SELECT, INSERT, UPDATE ON `limoapi`.`tbl_rt_user_devices`    TO 'limo_rt'@'localhost';
GRANT SELECT, INSERT, UPDATE ON `limoapi`.`tbl_rt_notifications`   TO 'limo_rt'@'localhost';
GRANT SELECT, INSERT, UPDATE ON `limoapi`.`tbl_rt_settings`        TO 'limo_rt'@'localhost';

-- Deploy-time migrator ---------------------------------------------------------------------
-- MariaDB/MySQL can't grant CREATE for "tables named tbl_rt_*", so this account gets CREATE
-- on the schema. It is used only by the deploy step, and the Alembic lint
-- (tests/realtime/test_migrations.py) refuses revisions that name any other table.
CREATE USER IF NOT EXISTS 'limo_rt_migrate'@'localhost' IDENTIFIED BY '1700limo_rt_migrate';
GRANT CREATE, ALTER, INDEX, DROP, REFERENCES, SELECT, INSERT, UPDATE, DELETE
      ON `limoapi`.* TO 'limo_rt_migrate'@'localhost';
-- Optional: after the first deploy, lock it down further by revoking DROP and granting it
-- back only when a migration needs it.

-- Laravel tables the migrator must read for foreign keys need nothing extra:
-- information_schema is visible for tables the account has privileges on.

FLUSH PRIVILEGES;

-- Check -----------------------------------------------------------------------------------
-- SHOW GRANTS FOR 'limoagent_ro'@'<app_host>';   -- must show SELECT only
-- SHOW GRANTS FOR 'limo_rt'@'<app_host>';
