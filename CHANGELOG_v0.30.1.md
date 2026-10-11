# v0.30.1 — FA 진입 오류 수정

- `postseason` 값이 `None`인 저장 게임에서 `/offseason/open-fa` 요청 시 발생하던 `AttributeError`를 수정했습니다.
- 포스트시즌 데이터가 없는 경우에도 안전하게 시즌 완료 조건을 검사합니다.
