## 改动摘要

<!-- 一句话说明这次要解决/交付什么；涉及的分层（server / esp32 / ui / docs）列一下。 -->

## 验证证据

<!-- 只贴「跑过的命令 + 真实输出结论」，不要写「应该没问题」。CI 覆盖不到的部分尤其要写。 -->

- 服务端自测：
  - [ ] `python server/test_receive.py`
  - [ ] `python server/test_photos.py`
  - [ ] `python server/test_events.py`
  - [ ] `python server/e2e_server_check.py`
  - [ ] `python server/e2e_ui_check.py`
- 固件构建（若改了 `main/`）：`.\build_idf.bat` → `BUILD_EXIT=0`
- 真机验收（若改了板端行为）：烧录版本、现象、串口关键读数

## 未验证项与风险

<!-- 没做的事 + 可能踩到的坑 + 回滚方式（git revert / 重新烧录旧 bin）。留空等于宣称「零风险」。 -->

- 未验证：
- 已知风险：
- 回滚方式：