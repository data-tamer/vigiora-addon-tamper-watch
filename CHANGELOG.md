# Changelog

## 1.0.0

First release.

- Takes one frame of each assigned camera's low-resolution stream every 30 seconds.
- Posts `tamper:covered`, `tamper:blurred` and `tamper:moved` as detections when a problem is seen on 3 checks in a row, so a camera's alert rule turns them into alerts, each with a confidence from 0.5 to 0.99. Posts `tamper:ok` as a plain result when it ends (never an alert). Each result carries the measured numbers and the limits.
- Needs Vigiora Server 1.7.4 (add-on alerts). On an older server it falls back to plain results and logs it once. A rate-limited post is kept and sent on the next check.
- Learns each camera's normal view (day, and night vision separately) from its first 10 checks, keeps it across restarts, and adapts it slowly while all is well.
- An unreachable camera gets no verdict.
- The moved test can be turned off for all cameras, per camera, or by tagging a camera `ptz` or `patrol`. The `relearn` camera setting makes it learn a new view.
