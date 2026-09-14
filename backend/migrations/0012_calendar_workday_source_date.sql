-- 补班改为按具体来源日期复制课表；调整类型收敛为调休、节假日、补班。
ALTER TABLE calendar_adjustments ADD COLUMN source_date TEXT;
ALTER TABLE calendar_adjustments ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';

DELETE FROM calendar_adjustments
WHERE adjustment_type NOT IN ('day_off', 'holiday', 'workday');

-- 旧版“按星期几”补班无法保留周次/单双周语义，要求重新按具体日期设置。
DELETE FROM calendar_adjustments
WHERE adjustment_type = 'workday' AND source_date IS NULL;

CREATE INDEX IF NOT EXISTS idx_calendar_adjustments_source_date
ON calendar_adjustments(source_date, adjustment_type);
