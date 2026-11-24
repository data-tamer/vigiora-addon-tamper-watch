# Changelog

## 1.0.0

First release.

- Takes one frame of each assigned camera's low-resolution stream every 30 seconds.
- Posts `tamper:covered`, `tamper:blurred` and `tamper:moved` when a problem is seen on 3 checks in a row, and `tamper:ok` when it ends. Each result carries the measured numbers and the limits.
- Learns each camera's normal view (day, and night vision separately) from its first 10 checks, keeps it across restarts, and adapts it slowly while all is well.
- An unreachable camera gets no verdict.
- The moved test can be turned off for all cameras, per camera, or by tagging a camera `ptz` or `patrol`. The `relearn` camera setting makes it learn a new view.

Known limit: on Vigiora Server 1.7 the results are events, not alerts. The server raises alerts only for detections and refuses detections from community add-ons, so an alert switch on a `tamper:` label has no effect yet.
