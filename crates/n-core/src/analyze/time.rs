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
            (
                st.year,
                st.month,
                st.day,
                st.hour,
                st.minute,
                st.second,
            )
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
        (1970, 1, 1, 0, 0)
    }
}

pub fn now_parts_with_seconds() -> (u16, u16, u16, u16, u16, u16) {
    #[cfg(windows)]
    {
        local::parts_with_seconds()
    }
    #[cfg(not(windows))]
    {
        (1970, 1, 1, 0, 0, 0)
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
