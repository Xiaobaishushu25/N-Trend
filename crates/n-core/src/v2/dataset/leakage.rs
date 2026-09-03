use crate::v2::dataset::DatasetRow;

#[derive(Debug)]
pub struct LeakageError { pub msg: String }

impl std::fmt::Display for LeakageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { write!(f, "leakage detected: {}", self.msg) }
}
impl std::error::Error for LeakageError {}

pub fn assert_no_leakage(rows: &[DatasetRow]) -> Result<(), LeakageError> {
    for r in rows {
        if let (Some(t), Some(e)) = (&r.trigger_bar_ts, &r.exit_ts) {
            if t > e {
                return Err(LeakageError{ msg: format!("trigger {} after exit {} for {}", t, e, r.event_id) });
            }
        }
        if r.schema_version.is_empty() {
            return Err(LeakageError{ msg: format!("missing schema_version for {}", r.event_id) });
        }
        if let (Some(trigger), Some(as_of)) = (r.trigger_bar_ts.as_deref(), r.context_as_of_ts.as_deref()) {
            if as_of > trigger {
                return Err(LeakageError{ msg: format!("context as_of {} is after trigger {} for {}", as_of, trigger, r.event_id) });
            }
        }
        if let (Some(as_of), Some(last60)) = (r.context_as_of_ts.as_deref(), r.context_last_60m_ts.as_deref()) {
            if last60 > as_of {
                return Err(LeakageError{ msg: format!("60m close {} is after context cutoff {} for {}", last60, as_of, r.event_id) });
            }
        }
    }
    Ok(())
}

pub fn time_split<'a>(rows: &'a [DatasetRow], split_ts: &str) -> (&'a [DatasetRow], &'a [DatasetRow]) {
    let idx = rows.iter().position(|r| r.trigger_bar_ts.as_deref().unwrap_or("") > split_ts).unwrap_or(rows.len());
    (&rows[..idx], &rows[idx..])
}

/// Walk-forward helper re-export for v2-dataset parity test
pub fn walk_forward_splits(n: usize, n_splits: usize) -> Vec<(usize,usize,usize,usize)> {
    crate::v2::model::walk_forward::walk_forward(n, n_splits).into_iter().map(|f| (f.train_start, f.train_end, f.valid_start, f.valid_end)).collect()
}
