/// Missing value policy — single source of truth
#[derive(Clone, Debug)]
pub struct MissingPolicy;

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct MissingMask(pub u32);

impl MissingMask {
    pub fn has(&self, bit: u32) -> bool { (self.0 & bit) != 0 }
    pub fn is_empty(&self) -> bool { self.0 == 0 }
}

/// Bits: 1=close_location/body missing, 2=volume_ratio missing, 4=ATR missing (discard), 8=OI missing (optional)
/// OI missing -> NULL + mask bit 8; ATR missing (bit 4) -> discard event (counted)
pub fn should_discard_due_to_missing(mask: u32) -> bool {
    (mask & 4) != 0
}
