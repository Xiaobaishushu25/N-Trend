#[cfg(windows)]
mod local {
    use std::mem::zeroed;

    #[repr(C)]
    struct SystemTime {
        year: u16,
        month: u16,
        day_of_week: u16,
        day: u16,
        hour: u16,
        minute: u16,
        second: u16,
        milliseconds: u16,
    }

    #[link(name = "Kernel32")]
    extern "system" {
        fn GetLocalTime(lpSystemTime: *mut SystemTime);
    }

    pub fn parts() -> (u16, u16, u16, u16, u16) {
        unsafe {
            let mut st: SystemTime = zeroed();
            GetLocalTime(&mut st);
            (st.year, st.month, st.day, st.hour, st.minute)
        }
    }

    pub fn parts_with_seconds() -> (u16, u16, u16, u16, u16, u16) {
        unsafe {
            let mut st: SystemTime = zeroed();
            GetLocalTime(&mut st);
            (st.year, st.month, st.day, st.hour, st.minute, st.second)
        }
    }
}

pub fn now_parts() -> (u16, u16, u16, u16, u16) {
    #[cfg(windows)]
    {
        local::parts()
    }
    #[cfg(not(windows))]
    {
        use chrono::{Datelike, Timelike};
        let now = chrono::Local::now();
        (
            now.year() as u16,
            now.month() as u16,
            now.day() as u16,
            now.hour() as u16,
            now.minute() as u16,
        )
    }
}

pub fn now_parts_with_seconds() -> (u16, u16, u16, u16, u16, u16) {
    #[cfg(windows)]
    {
        local::parts_with_seconds()
    }
    #[cfg(not(windows))]
    {
        use chrono::{Datelike, Timelike};
        let now = chrono::Local::now();
        (
            now.year() as u16,
            now.month() as u16,
            now.day() as u16,
            now.hour() as u16,
            now.minute() as u16,
            now.second() as u16,
        )
    }
}

pub fn now_minute() -> String {
    let (y, m, d, h, min) = now_parts();
    format!("{:04}{:02}{:02}_{:02}{:02}", y, m, d, h, min)
}

pub fn now_display() -> String {
    let (y, m, d, h, min) = now_parts();
    format!("{:04}-{:02}-{:02} {:02}:{:02}", y, m, d, h, min)
}
