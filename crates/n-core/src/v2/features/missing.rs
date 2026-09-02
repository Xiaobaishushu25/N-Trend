/// Missing value policy — single source of truth
#[derive(Clone, Debug)]
pub struct MissingPolicy;

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct MissingMask(pub u32);

impl MissingMask {
    pub fn has(&self, bit: u32) -> bool { (self.0 & bit) != 0 }
    pub fn is_empty(&self) -> bool { self.0 == 0 }
}

/// OI missing -> NULL + mask bit; ATR missing -> discard event (counted)
pub fn should_discard_due_to_missing(mask: u32) -> bool {
    // bit 4 reserved for ATR missing in setup
    (mask & 4) != 0
}
