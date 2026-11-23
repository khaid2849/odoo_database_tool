UPDATE ir_mail_server SET active = false;
UPDATE ir_cron SET active = false;
INSERT INTO ir_config_parameter (key, value) VALUES ('database.is_neutralized', true)
    ON CONFLICT (key) DO UPDATE SET value = true;
