# -*- coding: utf-8 -*-
"""
game_auto_v1_17.py

任意“已知页面”启动 + 安全恢复主界面版。

原则：
1. 已知页面：按明确规则退出/返回，直到主界面。
2. 未知页面：立即停止、保存726x1600现场，不猜着点。
3. 禁止 Android BACK。
4. 只使用：
   - 游戏左下角返回箭头
   - 弹窗自己的 X
   - 奖励页“点击继续”
   - 招募结果页安全空白
"""

import argparse
import datetime as dt
import time
import traceback
from pathlib import Path

import cv2

from game_auto_v1 import AutoBot, load_config
from tasks import GameTasks
from workflow_state import WorkflowState, TASK_TRANSITIONS


PROJECT_DIR = Path(__file__).resolve().parent


class RecoveryBot(AutoBot):

    def tap_game_back(self, label="游戏内左下角返回"):
        fr = self.frame()
        if fr is None:
            return False

        h, w = fr.shape[:2]
        x = int(round(w * 0.138))
        y = int(round(h * 0.964))

        self.log(f"[RECOVER] {label} frame=({x},{y})")

        return self.actions.tap_frame(
            x, y, fr.shape, label
        )

    def tap_battle_overview_back(self):
        """战力总览页使用该页左下角的返回箭头位置。"""
        fr = self.frame()
        if fr is None:
            return False

        h, w = fr.shape[:2]
        x = int(round(w * 0.138))
        y = int(round(h * 0.958))
        self.log(f"[RECOVER] 战力总览-左下角返回 frame=({x},{y})")
        return self.actions.tap_frame(
            x, y, fr.shape, "战力总览-左下角返回"
        )

    def tap_donation_x(self):
        fr = self.frame()
        if fr is None:
            return False

        h, w = fr.shape[:2]
        x = int(round(w * 0.918))
        y = int(round(h * 0.245))

        self.log(f"[RECOVER] 捐助弹窗-X frame=({x},{y})")

        return self.actions.tap_frame(
            x, y, fr.shape, "捐助弹窗-X"
        )

    def tap_crusade_reward_x(self):
        fr = self.frame()
        if fr is None:
            return False

        h, w = fr.shape[:2]
        x = int(round(w * 0.918))
        y = int(round(h * 0.330))

        self.log(f"[RECOVER] 讨伐奖励弹窗-X frame=({x},{y})")

        return self.actions.tap_frame(
            x, y, fr.shape, "讨伐奖励弹窗-X"
        )

    def tap_daily_x(self):
        fr = self.frame()
        if fr is None:
            return False

        h, w = fr.shape[:2]
        x = int(round(w * 0.944))
        is_wechat_daily = self.vision.match(
            fr,
            "page_daily_tasks_wechat",
        )
        y = int(round(h * (0.181 if is_wechat_daily else 0.222)))

        self.log(
            f"[RECOVER] 日常任务-X frame=({x},{y})"
        )

        return self.actions.tap_frame(
            x,
            y,
            fr.shape,
            "日常任务-X",
        )

    def tap_daily_reward_continue(self):
        fr = self.frame()
        if fr is None:
            return False

        h, w = fr.shape[:2]
        x = int(round(w * 0.50))
        y = int(round(h * 0.958))

        self.log(
            f"[RECOVER] 日常总奖励-点击继续 frame=({x},{y})"
        )

        return self.actions.tap_frame(
            x,
            y,
            fr.shape,
            "日常总奖励-点击继续",
        )

    def current_page(self, fr=None):
        fr = self.frame() if fr is None else fr
        if fr is None:
            return "no_frame", None

        # Training sheet covers the city but leaves city widgets visible.
        # Its 晋升 control and own back arrow are stronger page evidence than
        # background main-city templates or unreliable timer OCR.
        promote = self.vision.match(fr, "training_promote_button")
        sheet_back = self.vision.match(fr, "training_sheet_back")
        if promote and sheet_back:
            return "training_sheet", promote
        if (getattr(self, "training_sheet_expected", False) and sheet_back):
            return "training_sheet", sheet_back

        # Douyin can keep the same live-room picture after the 首页 tap.
        # Track that transition instead of clicking the same control twice.
        for page, anchor in (
            ("douyin_menu", "douyin_menu_heading"),
            ("douyin_search", "douyin_search_bar"),
            ("douyin_product", "douyin_product_title"),
            ("douyin_live", "douyin_live_prompt"),
        ):
            found = self.vision.match(fr, anchor)
            if found:
                if page == "douyin_live" and getattr(self, "douyin_stage", "") == "home_tapped":
                    return "douyin_feed", found
                return page, found
        if getattr(self, "startup_platform", "auto") == "douyin":
            home = self.vision.match(fr, "douyin_home_icon")
            if home:
                return "douyin_home", home

        # Priority matters: overlays first, underlying pages later.
        # The welcome dialog leaves the city navigation visible behind it;
        # classify it before the main-city anchor.
        for page, anchor in (
            ("startup_promo", "startup_game_promo_title"),
            ("welcome", "welcome_claim"),
            ("startup_loading", "startup_game_loading"),
            ("wechat_recent", "startup_wechat_recent_title"),
            ("wechat_chat", "startup_wechat_chat_title"),
            ("wechat_list", "startup_wechat_list_title"),
            ("android_home", "startup_wechat_home_icon"),
        ):
            found = self.vision.match(fr, anchor,
                                      threshold=.80 if page == "welcome" else None)
            if found:
                return page, found
        if self.vision.match(fr, "page_minxin"):
            if self.vision.match(fr, "minxin_skill_cost"):
                return "minxin_skill", None
            return "minxin", None
        lubu_target = self.vision.match(fr, "lubu_march_target")
        lubu_buff = self.vision.match(fr, "lubu_buff")
        lubu_signature = bool(lubu_target or lubu_buff)
        checks = [
            # Interrupting popups can cover the city at any time; recognize
            # them before any underlying page and close through their own X.
            ("popup_forge_offer", "popup_forge_offer_close"),
            ("popup_alliance_bulletin", "popup_alliance_bulletin_close"),
            ("congrats", "page_congrats"),
            ("lubu_start", "lubu_start_dialog"),
            ("lubu_target", "lubu_map_detail"),
            ("lubu_war", "lubu_war"),
            ("chest_ratio", "page_chest_ratio"),
            ("chest_target", "page_chest_target"),
            ("chest_march", "chest_equipment"),
            ("chest_march", "chest_equipment_blue"),
            ("activity", "page_activity"),
            ("beacon_battle_start", "beacon_battle_start_button"),
            ("beacon_battle_running", "beacon_battle_timer"),
            ("beacon", "page_beacon_alt"),
            ("resident_continue", "resident_continue"),
            ("resident_dialog", "page_resident_event"),
            ("mount_visit", "page_mount_visit"),
            ("daily_total_reward", "page_daily_total_reward"),
            ("daily_tasks", "page_daily_tasks_wechat"),
            ("daily_tasks", "page_daily_tasks"),
            ("beacon_reward", "page_beacon_reward"),
            ("beacon", "page_beacon"),

            # 烽火台事件中间页：必须排在main前面，
            # 防止底层世界地图被误认为已经回到主界面。
            ("beacon_resource_popup", "world_resource_popup"),
            ("beacon_resource_popup", "world_resource_popup_alt"),
            ("beacon_resource_march", "page_resource_march"),
            ("beacon_resource_march", "page_resource_march_alt"),
            ("beacon_excavation_popup", "beacon_excavation_popup"),
            ("beacon_rally_popup", "world_rally_popup"),
            ("beacon_rally_time", "page_rally_time_modal"),
            ("beacon_hero_select", "page_hero_select"),
            ("beacon_rally_empty", "rally_slots_empty"),
            ("beacon_rally_zhurong", "rally_zhurong_solo"),
            ("beacon_march_form", "beacon_march_controls"),

            # v1.17 新页面/覆盖层，必须排在main之前。
            ("stamina_more", "page_stamina_more"),
            ("profile", "page_profile_wechat"),
            ("profile", "page_profile"),
            ("profile", "profile_stamina_plus"),
            ("title", "page_title"),
            ("battle_overview", "page_battle_overview"),
            ("auto_join", "page_auto_join_wechat"),
            ("auto_join", "page_auto_join"),
            ("alliance_war", "page_alliance_war"),
            ("search_panel", "page_search_panel"),
            ("merchant", "page_merchant"),
            ("caravan_dialog", "caravan_dialog_title"),
            ("caravan", "caravan_page_title"),
            ("study_tree", "page_study_tree"),
            ("study_home", "page_study_home"),
            ("queue_panel", "queue_panel_training"),

            ("main", "main_alliance"),
            ("welcome", "welcome_claim"),
            ("reward_result", "reward_continue"),

            ("donation_popup", "page_donation_popup"),
            ("donation_popup", "donate_blue"),
            ("donation_popup", "donate_grey"),

            ("recruit_result", "page_recruit_result"),
            ("crusade_reward_popup", "page_crusade_reward_popup"),
            ("crusade_reward_popup", "crusade_claim"),

            ("alliance_home", "page_alliance_home"),
            ("alliance_tech", "page_alliance_tech"),
            ("alliance_gift", "page_alliance_gift"),
            ("crusade", "page_crusade_wechat"),
            ("crusade", "page_crusade"),
            ("generals", "page_generals"),
            ("recruit_no_free", "page_recruit_no_free"),
            ("recruit", "page_recruit_wechat"),
            ("recruit", "page_recruit"),
            # WeChat客户端的招募页布局与旧整页模板差异较大，
            # 但免费按钮是页面内稳定且有明确语义的锚点。
            ("recruit", "recruit_free"),
        ]

        for page, tmpl in checks:
            if page in {"chest_ratio", "chest_march"} and lubu_signature:
                ratio = self.vision.match(fr, "lubu_ratio")
                controls = self.vision.match(fr, "lubu_march_controls")
                if ratio:
                    return "lubu_ratio", ratio
                if controls:
                    return "lubu_march", controls
            # This button template is only a fallback page anchor; keep a
            # stricter threshold because its search region can contain other
            # green controls on unrelated screens.
            m = self.vision.match(
                fr,
                tmpl,
                threshold=0.90 if tmpl == "recruit_free" else None,
            )
            if m:
                return page, m

        # Shared resource/ratio widgets also appear on older combat pages.
        # Classify those as Lv Bu only when its target or dedicated faction
        # banner is visible behind the panel.
        # The training selection sheet has a distinctive wide blue button;
        # use it as a visual fallback when OCR returns no text.
        h, w = fr.shape[:2]
        x1, x2 = round(w * .49), round(w * .99)
        y1, y2 = round(h * .835), round(h * .91)
        patch = fr[y1:y2, x1:x2]
        if patch.size:
            hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
            blue = cv2.inRange(hsv, (85, 55, 65), (135, 255, 255))
            contours, _ = cv2.findContours(blue, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                bx, by, bw, bh = cv2.boundingRect(contour)
                if (bw >= round(w * .32) and
                        round(h * .025) <= bh <= round(h * .085) and
                        by + bh > (y2 - y1) * .45):
                    self.log("[VISION] 底部宽蓝色训练按钮确认训兵配置页（OCR无关）")
                    return "training_sheet", None
        # Troop training is a bottom sheet over the city. On OCR failure in
        # its timer, recovery must still know how to leave this known page.
        try:
            from local_ocr import read_text
            sheet = read_text(fr, (0, 785, 726, 1480), scale=2)
            sheet_text = "".join(sheet.get("text", "").split())
            if ("立即完成" in sheet_text and
                    ("训练" in sheet_text or "训练中" in sheet_text) and
                    any(camp in sheet_text for camp in ("步兵", "弓兵", "骑兵"))):
                return "training_sheet", None
        except Exception as exc:
            self.log(f"[VISION] 训兵页恢复识别失败：{exc}")
        if any(self.vision.match(fr, name) for name in
               ("barracks_training_hand", "barracks_training_hand_alt")):
            return "training_building_detail", None
        try:
            from local_ocr import read_text
            camp_title = "".join(read_text(fr, (250, 610, 470, 700), scale=2)
                                  .get("text", "").split())
            camp_actions = "".join(read_text(fr, (420, 850, 560, 965), scale=2)
                                    .get("text", "").split())
            if (any(name in camp_title for name in ("步兵营", "弓兵营", "骑兵营"))
                    and "训练" in camp_actions):
                return "training_building_detail", None
        except Exception as exc:
            self.log(f"[VISION] 兵营详情恢复识别失败：{exc}")
        return "unknown", None

    def recover_one_step(self):
        fr = self.frame()
        if fr is None:
            return False, "no_frame"

        page, match = self.current_page(fr)

        self.log(f"[RECOVER] 当前页面：{page}")

        if page in {"minxin", "minxin_skill"}:
            y = 504 if page == "minxin_skill" else 322
            ok = self.actions.tap_frame(round(673 * fr.shape[1] / 726),
                                        round(y * fr.shape[0] / 1600),
                                        fr.shape, "民心-关闭当前弹窗")
            return ok, "acted" if ok else "tap_failed"

        if page == "training_building_detail":
            # The barracks detail popover has no back arrow. Tap the clear map
            # area away from all buildings and contextual controls.
            h, w = fr.shape[:2]
            ok = self.actions.tap_frame(round(w * .91), round(h * .77),
                                        fr.shape, "兵营详情-安全空白关闭")
            return ok, "acted" if ok else "tap_failed"

        if page in {"popup_forge_offer", "popup_alliance_bulletin"}:
            if match is None or not self.actions.tap_match(
                    match, fr.shape, f"{page}-弹窗X关闭"):
                return False, "popup_close_tap_failed"
            return True, "acted"

        if page == "congrats":
            ok = GameTasks(self)._handle_congratulations(already_open=True)
            return ok, "acted" if ok else "congrats_unfinished"
        if page == "chest_ratio":
            h, w = fr.shape[:2]
            ok = self.actions.tap_frame(round(w*.925), round(h*.295), fr.shape, "兵力配比-X关闭")
            return ok, "acted" if ok else "tap_failed"
        if page in {"lubu_ratio", "lubu_start"}:
            ok = self.actions.tap_frame(round(fr.shape[1]*.93), match.center[1], fr.shape, "吕布弹窗-关闭")
            return ok, "acted" if ok else "tap_failed"
        if page == "lubu_target":
            ok = self.actions.tap_frame(round(fr.shape[1]*.8), round(fr.shape[0]*.50), fr.shape, "吕布目标-关闭详情")
            return ok, "acted" if ok else "tap_failed"
        if page in {"lubu_march", "lubu_war"}:
            ok = self.tap_game_back("吕布页面-返回")
            return ok, "acted" if ok else "tap_failed"
        if page == "chest_target":
            h, w = fr.shape[:2]
            ok = self.actions.tap_frame(round(w*.70), round(h*.57), fr.shape, "群雄目标-弹窗外关闭")
            return ok, "acted" if ok else "tap_failed"
        if page == "chest_march":
            ok = self.tap_game_back("打宝箱编队-返回")
            return ok, "acted" if ok else "tap_failed"
        if page == "activity":
            h, w = fr.shape[:2]
            ok = self.actions.tap_frame(round(w*.086), round(h*.942), fr.shape, "活动-返回")
            return ok, "acted" if ok else "tap_failed"

        if page in {"beacon_resource_popup", "beacon_excavation_popup"}:
            h, w = fr.shape[:2]
            ok = self.actions.tap_frame(round(w*.5), round(h*.72), fr.shape,
                                        "烽火台目标-弹窗外关闭")
            return ok, "acted" if ok else "tap_failed"

        if page in {"beacon_resource_march", "beacon_march_form"}:
            ok = self.tap_game_back("烽火台编队-返回")
            return ok, "acted" if ok else "tap_failed"

        if page in {"resident_continue", "resident_dialog"}:
            ok = GameTasks(self)._finish_resident_event()
            return ok, "acted" if ok else "resident_unfinished"

        if page == "daily_total_reward":
            self.tap_daily_reward_continue()
            return True, "acted"

        if page == "daily_tasks":
            self.tap_daily_x()
            return True, "acted"

        if page == "beacon_reward":
            fr = self.frame()
            h, w = fr.shape[:2]
            self.log("[RECOVER] 烽火台奖励页 -> 点击继续")
            self.actions.tap_frame(
                int(w * 0.50),
                int(h * 0.958),
                fr.shape,
                "烽火台奖励-点击继续",
            )
            return True, "acted"

        if page == "beacon":
            self.tap_game_back("烽火台-返回主界面")
            return True, "acted"

        if page == "beacon_battle_start":
            start = self.vision.match(fr, "beacon_battle_start_button")
            if not start or not self.actions.tap_match(start, fr.shape, "beacon battle start"):
                return False, "tap_failed"
            if not self.wait_template("beacon_battle_timer", 8):
                return False, "battle_start_unconfirmed"
            return self.actions.tap_frame(
                int(fr.shape[1] * .138), int(fr.shape[0] * .964),
                fr.shape, "beacon battle exit"
            ), "acted"

        if page == "beacon_battle_running":
            return self.actions.tap_frame(
                int(fr.shape[1] * .138), int(fr.shape[0] * .964),
                fr.shape, "beacon battle exit"
            ), "acted"

        if page == "stamina_more":
            # 只点弹窗上方安全空白区域。
            # 屏幕最右上角圆形“-”为永久禁区，绝不点击。
            fr = self.frame()
            h, w = fr.shape[:2]
            x = int(round(w * 0.50))
            y = int(round(h * 0.205))
            self.log(
                f"[RECOVER] 获取更多弹窗 -> 安全空白退出 "
                f"frame=({x},{y})"
            )
            self.actions.tap_frame(
                x, y, fr.shape,
                "获取更多-安全空白退出",
            )
            return True, "acted"

        if page == "caravan_dialog":
            # Confirmed X in the supplied 商队赠礼 dialog, not Android BACK.
            ok = self.actions.tap_frame(round(fr.shape[1] * .925),
                                        round(fr.shape[0] * .274), fr.shape,
                                        "同盟商队-关闭赠礼弹窗")
            return ok, "acted" if ok else "tap_failed"

        if page == "training_sheet":
            back = self.vision.match(fr, "training_sheet_back")
            if back is None:
                self.fail_snapshot("training_sheet_back_missing")
                return False, "back_missing"
            ok = self.actions.tap_match(back, fr.shape, "训兵页-左下返回")
            if ok:
                self.training_sheet_expected = False
            return ok, "acted" if ok else "tap_failed"

        if page in {"profile","title","alliance_war","merchant","caravan","study_tree","study_home","mount_visit"}:
            self.tap_game_back(f"{page}-返回")
            return True, "acted"

        if page == "battle_overview":
            if not self.tap_battle_overview_back():
                return False, "tap_failed"
            return True, "acted"

        if page == "auto_join":
            fr = self.frame()
            h, w = fr.shape[:2]
            # WeChat 版弹窗整体上移，X 位于 y≈20.4%；旧版约 y≈24.5%。
            yr = 0.204 if match and match.name == "page_auto_join_wechat" else 0.245
            self.actions.tap_frame(
                int(w * 0.925), int(h * yr), fr.shape, "自动加集结-X"
            )
            return True, "acted"

        if page == "search_panel":
            # 搜索面板无独立X；点击左侧搜索图标可收起。
            fr=self.frame(); h,w=fr.shape[:2]
            self.actions.tap_frame(int(w*0.075),int(h*0.628),fr.shape,"搜索面板-收起")
            return True, "acted"

        if page == "queue_panel":
            fr=self.frame(); h,w=fr.shape[:2]
            self.actions.tap_frame(int(w*0.72),int(h*0.72),fr.shape,"综合队列-收起")
            return True, "acted"

        if page == "main":
            return True, "main"

        if page == "douyin_home":
            ok = self.actions.tap_match(match, fr.shape, "桌面-打开抖音")
            return ok, "acted" if ok else "tap_failed"

        if page == "douyin_product":
            ok = self.actions.tap_frame(round(fr.shape[1] * .075),
                                        round(fr.shape[0] * .080), fr.shape,
                                        "抖音商品页-左上X关闭")
            return ok, "acted" if ok else "tap_failed"

        if page == "douyin_search":
            ok = self.actions.tap_frame(round(fr.shape[1] * .933),
                                        round(fr.shape[0] * .084), fr.shape,
                                        "抖音搜索页-右上X关闭")
            return ok, "acted" if ok else "tap_failed"

        if page == "douyin_live":
            ok = self.actions.tap_frame(round(fr.shape[1] * .090),
                                        round(fr.shape[0] * .952), fr.shape,
                                        "抖音直播页-首页")
            if ok:
                self.douyin_stage = "home_tapped"
            return ok, "acted" if ok else "tap_failed"

        if page == "douyin_feed":
            menu = self.vision.match(fr, "douyin_feed_menu")
            if menu is None:
                self.fail_snapshot("douyin_feed_menu_missing")
                return False, "menu_missing"
            ok = self.actions.tap_match(menu, fr.shape, "抖音首页-左上菜单")
            return ok, "acted" if ok else "tap_failed"

        if page == "douyin_menu":
            icon = self.vision.match(fr, "douyin_game_icon")
            if icon is None:
                self.fail_snapshot("douyin_game_icon_missing")
                return False, "game_icon_missing"
            ok = self.actions.tap_match(icon, fr.shape, "抖音常用小程序-三国冰河")
            if ok:
                self.douyin_stage = "game_opened"
                self.douyin_game_opened_at = time.time()
            return ok, "acted" if ok else "tap_failed"

        if page == "android_home":
            return (self.actions.tap_match(match, fr.shape, "桌面-打开微信"), "acted")

        if page == "wechat_chat":
            # The supplied conversation has its own visible top-left arrow.
            ok = self.actions.tap_frame(round(fr.shape[1] * .055),
                                        round(fr.shape[0] * .079), fr.shape,
                                        "微信会话-左上返回聊天列表")
            return ok, "acted" if ok else "tap_failed"

        if page == "wechat_list":
            ok = self.actions.swipe_frame(round(fr.shape[1] * .50),
                                          round(fr.shape[0] * .30),
                                          round(fr.shape[1] * .50),
                                          round(fr.shape[0] * .73),
                                          fr.shape, duration_ms=550,
                                          label="微信聊天列表-下拉最近小程序")
            return ok, "acted" if ok else "swipe_failed"

        if page == "wechat_recent":
            icon = self.vision.match(fr, "startup_game_recent_icon")
            if not icon:
                self.log("[STARTUP] 最近页已确认，但未找到三国小程序图标")
                self.fail_snapshot("wechat_game_icon_missing")
                return False, "game_icon_missing"
            ok = self.actions.tap_match(icon, fr.shape, "微信最近-打开三国小程序")
            return ok, "acted" if ok else "tap_failed"

        if page == "startup_promo":
            # The paid offer has a dedicated X; never touch its recharge button.
            ok = self.actions.tap_frame(round(fr.shape[1] * .942),
                                        round(fr.shape[0] * .168), fr.shape,
                                        "最强群攻充值弹窗-X关闭")
            return ok, "acted" if ok else "tap_failed"

        if page == "startup_loading":
            return True, "waiting"

        if page == "welcome":
            ok = self.actions.tap_match(match, fr.shape, "欢迎回家-领取")
            return ok, "acted" if ok else "tap_failed"

        if page == "reward_result":
            self.actions.tap_match(
                match, fr.shape, "奖励-点击继续"
            )
            return True, "acted"

        if page == "donation_popup":
            self.tap_donation_x()
            return True, "acted"

        if page == "recruit_result":
            h, w = fr.shape[:2]
            self.log("[RECOVER] 招募结果页 -> 安全空白退出")
            self.actions.tap_frame(
                int(w * 0.10),
                int(h * 0.70),
                fr.shape,
                "招募结果-安全空白退出",
            )
            return True, "acted"

        if page == "crusade_reward_popup":
            self.tap_crusade_reward_x()
            return True, "acted"

        if page in {
            "alliance_home",
            "alliance_tech",
            "alliance_gift",
            "crusade",
            "generals",
            "recruit_no_free",
            "recruit",
        }:
            self.tap_game_back(f"{page}-返回")
            return True, "acted"

        # Unknown: DO NOT CLICK.
        self.fail_snapshot("unknown_start_page")
        self.log(
            "[STOP] 当前是未支持页面。已保存现场，"
            "程序不会猜着点。"
        )
        return False, "unknown"

    def normalize_to_main(self, timeout=30, startup=False):
        """
        From any supported page, safely unwind to main.
        Unknown page -> stop.
        """
        deadline = time.time() + timeout
        previous_page = None
        unchanged_page_count = 0
        transition_until = 0.0
        last_wait_log = 0.0

        while time.time() < deadline:
            fr = self.frame()
            if fr is None:
                time.sleep(0.10)
                continue

            page, _ = self.current_page(fr)

            if page == "main":
                self.log("[OK] 已恢复到主界面")
                return True

            if (startup and page == "douyin_menu" and
                    getattr(self, "douyin_stage", "") == "game_opened"):
                age = time.time() - getattr(self, "douyin_game_opened_at", 0)
                if age < 8:
                    time.sleep(.8)
                    continue
                # A failed launch gets one fresh-frame retry, never an
                # immediate second tap against the same menu animation.
                self.douyin_stage = ""

            if startup and (page == "startup_loading" or
                            (page == "unknown" and time.time() < transition_until)):
                now = time.time()
                if now - last_wait_log > 5:
                    self.log(f"[STARTUP] 等待游戏/微信页面加载；当前={page}")
                    last_wait_log = now
                time.sleep(.8)
                continue

            if page == previous_page:
                unchanged_page_count += 1
            else:
                previous_page = page
                unchanged_page_count = 0

            if unchanged_page_count >= 2:
                reason = f"normalize_no_progress_{page}"
                self.fail_snapshot(reason)
                self.log(
                    f"[STOP] 连续点击后页面仍为 {page}，"
                    "已保存现场并停止恢复。"
                )
                return False

            ok, result = self.recover_one_step()

            if not ok:
                return False

            if startup and result == "acted":
                transition_until = time.time() + (75 if page in {"wechat_recent", "douyin_menu"}
                                                  else 12 if page == "android_home"
                                                  else 5)

            time.sleep(0.55)

        self.fail_snapshot("normalize_to_main_timeout")
        self.log("[STOP] 30秒仍未恢复主界面，停止执行。")
        return False

    # Override the old unsafe fallback completely.
    def ensure_main(self):
        return self.normalize_to_main(timeout=20)


class RecoveryTasks(GameTasks):
    def startup_to_main(self):
        self.bot.log("[FLOW] 启动页面归一化")
        return self.bot.normalize_to_main(timeout=120, startup=True)


def parse_args():
    ap = argparse.ArgumentParser(
        description="游戏自动化 v1.17：任意已知页面启动"
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="真正执行点击；不加时只识别当前页面。",
    )
    ap.add_argument(
        "--task",
        choices=[
            "extras",
            "alliance",
            "crusade",
            "recruit",
            "daily",
            "beacon",
            "title",
            "stamina",
            "auto_rally",
            "queue",
            "merchant",
            "caravan",
            "rogue",
            "gather",
            "gather_recall",
            "lubu",
            "resident",
            "mount",
            "minxin",
            "chest",
            "template_capture",
            "all",
        ],
        default="all",
    )
    ap.add_argument("--platform", choices=["both", "wechat", "douyin", "auto"],
                    default="both", help="默认先完成微信端，再完成抖音端")
    ap.add_argument("--gather-level", type=int, default=None)
    ap.add_argument("--lubu-minutes", type=float, default=20, help="打吕布最长运行分钟数，0到120之间")
    ap.add_argument("--lubu-formations", default="1,2,3,4,5,6", help="依次使用的预设编队编号，如1,2,3")
    ap.add_argument("--gather-plan", type=str, default=None, help="农舍,伐木,煤,铁，例如 2,1,1,1")
    ap.add_argument("--rogue-level", type=int, default=None)
    ap.add_argument("--rogue-count", type=int, default=None)
    ap.add_argument("--chest-count", type=int, choices=range(6), default=2,
                    help="群雄混战本轮讨伐次数，默认2次，每次间隔至少60秒；0跳过")
    ap.add_argument("--stamina-add", type=int, default=None, help="显式补体力接口：指定要补多少体力；不传则绝不自动使用体力道具")
    ap.add_argument("--capture", choices=["queue", "alliance_gift", "stamina", "daily", "beacon"], default="queue")
    return ap.parse_args()


def scan(bot):
    fr = bot.frame()
    if fr is None:
        return 2

    page, match = bot.current_page(fr)

    bot.log(f"[SCAN] 当前页面分类：{page}")

    if match:
        bot.log(
            f"[SCAN] 锚点：{match.name} "
            f"score={match.score:.3f}"
        )

    if page == "unknown":
        bot.fail_snapshot("scan_unknown_page")
        bot.log(
            "[SCAN] 未知页面已保存截图；"
            "没有执行任何点击。"
        )

    return 0



def _ask_int(prompt, default=0, min_value=0, max_value=None):
    while True:
        raw=input(f"{prompt} [{default}]：").strip()
        if raw=="": return default
        try: v=int(raw)
        except ValueError:
            print("请输入整数。")
            continue
        if v<min_value or (max_value is not None and v>max_value):
            print("输入超出范围。")
            continue
        return v


def prepare_runtime_options(args, task_name):
    opts={}
    need_gather=task_name in {"gather","all"}
    need_rogue=task_name in {"rogue","all"}

    # 体力道具补充改为“显式接口”：
    # - all / stamina 均不再主动询问用户。
    # - 默认 stamina_add=0，即只检查每日12:00/20:00仓库体力领取。
    # - 只有用户明确传入 --stamina-add N 时才使用体力道具。
    opts["stamina_add"] = max(0, int(args.stamina_add or 0))
    opts["chest_count"] = getattr(args, "chest_count", 2)
    opts["lubu_minutes"] = getattr(args, "lubu_minutes", 20)
    opts["lubu_formations"] = [int(s.strip()) for s in getattr(args, "lubu_formations", "1,2,3,4,5,6").split(",")]

    if need_rogue:
        # 自动化模式禁止阻塞式 input。
        # 未指定参数时使用默认值。
        opts["rogue_level"] = args.rogue_level if args.rogue_level is not None else 35
        opts["rogue_count"] = args.rogue_count if args.rogue_count is not None else 0

    if need_gather:
        # 自动化模式禁止等待人工输入。
        opts["gather_level"] = args.gather_level if args.gather_level is not None else 8
        if args.gather_plan:
            parts=[int(x.strip()) for x in args.gather_plan.split(',')]
            if len(parts)!=4: raise ValueError("--gather-plan 必须是4个数字：农舍,伐木,煤,铁")
        else:
            # 默认不主动占用6队采集。
            # 用户需要采集时通过 --gather-plan 指定。
            parts=[0,0,0,0]
        if sum(parts)>6:
            raise ValueError(f"资源采集计划合计{sum(parts)}队，超过6队")
        opts["gather_plan"]={"farm":parts[0],"wood":parts[1],"coal":parts[2],"iron":parts[3]}

    return opts

def run_live(bot, task_name):
    tasks = RecoveryTasks(bot)
    failed_modules = []
    waiting_modules = []

    def run_module(name, fn, attempt):
        state = WorkflowState(bot, f"{name}#{attempt}", TASK_TRANSITIONS)
        frame = bot.frame()
        page, match = bot.current_page(frame) if frame is not None else ("no_frame", None)
        if page in ("unknown", "no_frame"):
            state.move("unknown", f"入口画面={page}")
            bot.fail_snapshot(f"{name}_entry_unknown")
            return False
        state.move("observing", f"入口画面={page}" +
                   (f" anchor={match.name} score={match.score:.3f}" if match else ""))
        state.move("acting", f"已确认入口画面={page}，进入任务流程")
        try:
            bot.task_waiting_reason = None
            bot.task_skip_reason = None
            bot.task_completion_evidence = None
            result = fn()
        except Exception as exc:
            state.move("unknown", f"任务异常={type(exc).__name__}: {exc}")
            raise
        if not result:
            state.move("unknown", "任务流程未确认业务后置状态")
            return False
        frame = bot.frame()
        page, match = bot.current_page(frame) if frame is not None else ("no_frame", None)
        if page in ("unknown", "no_frame"):
            state.move("unknown", f"任务返回后画面={page}")
            bot.fail_snapshot(f"{name}_result_unknown")
            return False
        if getattr(bot, "task_waiting_reason", None):
            state.move("waiting", f"{bot.task_waiting_reason}；返回画面={page}")
            waiting_modules.append(name)
            return True
        if getattr(bot, "task_skip_reason", None):
            state.move("skipped", bot.task_skip_reason)
            return True
        state.move("verified", f"任务返回成功且后置画面={page}" +
                   (f" anchor={match.name} score={match.score:.3f}" if match else ""))
        state.move("complete", getattr(bot, "task_completion_evidence", None)
                   or "任务返回成功，后置画面已重新识别")
        return True

    if not tasks.startup_to_main():
        return 2
    from account_records import identify_current_account
    if not identify_current_account(tasks):
        bot.log("[STOP] 账号识别后未确认安全返回主界面")
        return 2

    if task_name == "extras":
        if not tasks.handle_main_extras():
            bot.log("[STOP] 主界面额外任务失败")
            return 2

        bot.log("[DONE] extras完成")
        return 0

    if not tasks.handle_main_extras():
        bot.log("[FAIL] 主界面额外任务失败")
        if task_name != "all":
            bot.log("[STOP] 主界面额外任务失败，不继续单项任务")
            return 2
        failed_modules.append("main_extras")
        bot.log("[FLOW] 全任务模式将尝试恢复后继续后续模块")
        bot.normalize_to_main(timeout=20)

    if task_name == "template_capture":
        try:
            from template_capture import run_template_capture
            bot.log(f"[FLOW] 模板采集模式 target={getattr(bot, 'capture_target', 'queue')}")
            run_template_capture(bot, capture_target=getattr(bot, 'capture_target', 'queue'))
            return 0
        except Exception as e:
            bot.log(f"[STOP] 模板采集失败: {e}")
            return 2

    task_map = {
        "alliance": tasks.alliance,
        "crusade": tasks.crusade,
        "recruit": tasks.recruit,
        "daily": tasks.daily_tasks,
        "beacon": tasks.beacon,
        "title": tasks.title_daily,
        "stamina": tasks.stamina,
        "auto_rally": tasks.auto_rally,
        "queue": tasks.queue_maintenance,
        "merchant": tasks.merchant,
        "caravan": tasks.alliance_caravan,
        "rogue": tasks.rogue,
        "gather": tasks.gather,
        "gather_recall": tasks.gather_recall,
        "lubu": tasks.lubu,
        "resident": tasks.resident_event,
        "mount": tasks.mount_visit,
        "minxin": tasks.minxin,
        "chest": tasks.chest_battles,
    }

    if task_name == "all":
        sequence = [
            ("crusade", tasks.crusade),
            ("alliance", tasks.alliance),
            ("caravan", tasks.alliance_caravan),
            ("mount", tasks.mount_visit),
            ("minxin", tasks.minxin),
            # 短时/每日领取优先。
            ("title", tasks.title_daily),
            ("stamina", tasks.stamina),
            ("auto_rally", tasks.auto_rally),
            ("recruit", tasks.recruit),
            ("daily", tasks.daily_tasks),
            ("queue", tasks.queue_maintenance),
            ("merchant", tasks.merchant),
            ("chest", tasks.chest_battles),
            ("beacon", tasks.beacon),
            # 流寇必须放在长期资源采集之前，避免6队被采集占满。
            ("rogue", tasks.rogue),
            # 居民事件在城镇视图处理，处理后再进行长期采集。
            ("resident", tasks.resident_event),
            ("mount_recheck", tasks.mount_visit),
            # 长时间资源采集最后执行。
            ("gather", tasks.gather),
        ]
    else:
        sequence = [
            (task_name, task_map[task_name]),
        ]

    records = getattr(bot, "account_records", None)
    if records:
        for name, _ in sequence:
            records.record(name, "scheduled", "本次运行已排程，尚未执行")

    for name, fn in sequence:
        if task_name == "all" and not bot.normalize_to_main(timeout=20):
            bot.log(
                f"[SKIP] 模块 {name}：当前页面未能安全恢复到主界面；"
                "继续检查后续模块"
            )
            if records:
                records.record(name, "unknown", "页面未能安全恢复，未执行")
            failed_modules.append(name)
            continue

        try:
            # 周期礼物可能在任何一个模块执行期间出现，每次回主界面补查。
            if bot.is_main() and not tasks.handle_main_extras():
                failed_modules.append(f"extras_before_{name}")
                if not bot.normalize_to_main(timeout=20):
                    failed_modules.append(name)
                    continue
            ok = run_module(name, fn, 1)
        except Exception as e:
            bot.log(f"[ERROR] 模块 {name} 抛出异常：{type(e).__name__}: {e}")
            bot.log(traceback.format_exc().rstrip())
            ok = False

        if not ok:
            bot.log(f"[FAIL] 模块 {name} 执行失败")
            recovered = bot.normalize_to_main(timeout=20)
            if task_name == "all" and name not in {"beacon", "lubu", "caravan", "queue", "mount", "mount_recheck"}:
                # A failed click can leave the task on a different screen or
                # shift the map. Re-capture and classify from scratch before
                # one bounded retry; never reuse coordinates from the first run.
                retry_frame = bot.frame()
                retry_page, retry_match = (
                    bot.current_page(retry_frame)
                    if retry_frame is not None else ("unknown", None)
                )
                bot.log(
                    f"[RETRY] {name}: 已重新截图并识别页面={retry_page}"
                    + (f" anchor={retry_match.name} score={retry_match.score:.3f}"
                       if retry_match else "")
                )
                if not recovered and retry_page == "unknown":
                    bot.log(f"[RETRY] {name}: 页面未知，再尝试一次安全归位")
                    recovered = bot.normalize_to_main(timeout=12)
                    if recovered:
                        retry_frame = bot.frame()
                        retry_page, retry_match = (
                            bot.current_page(retry_frame)
                            if retry_frame is not None else ("unknown", None)
                        )
                        bot.log(
                            f"[RETRY] {name}: 归位后新截图页面={retry_page}"
                            + (f" anchor={retry_match.name} score={retry_match.score:.3f}"
                               if retry_match else "")
                        )
                if recovered and retry_page != "unknown":
                    try:
                        ok = run_module(name, fn, 2)
                    except Exception as e:
                        bot.log(
                            f"[RETRY ERROR] 模块 {name}: "
                            f"{type(e).__name__}: {e}"
                        )
                        bot.log(traceback.format_exc().rstrip())
                        ok = False
                    if ok:
                        bot.log(f"[RETRY OK] 模块 {name} 重试后完成")
                        continue
                    bot.log(f"[RETRY FAIL] 模块 {name} 重新识别后仍失败")
                elif retry_page == "unknown":
                    bot.log(f"[RETRY SKIP] 模块 {name}：新截图页面仍未知")
                else:
                    bot.log(f"[RETRY SKIP] 模块 {name}：无法安全归位到主界面")
            elif task_name == "all" and name in {"beacon", "lubu", "queue"}:
                bot.log(f"[RETRY SKIP] {name}：已点击的派遣／晋升／训练结果可能未确认，"
                        "本轮不重复执行")
            elif task_name == "all" and name in {"mount", "mount_recheck"}:
                bot.log(f"[RETRY SKIP] {name}：入口状态未知；保留现场，等待后续回访")

            failed_modules.append(name)

            # 单项运行维持原有停止语义；全任务模式会恢复并继续。
            if task_name != "all":
                bot.log("[STOP] 单项任务失败，已尝试安全恢复")
                return 2
            if recovered:
                bot.log(f"[FLOW] 模块 {name} 失败后已恢复主界面，继续下一个模块")
            else:
                bot.log(
                    f"[FLOW] 模块 {name} 失败后暂未恢复主界面；"
                    "后续模块会先尝试恢复，未恢复时将安全跳过"
                )

    if not bot.normalize_to_main(timeout=20):
        bot.log(
            "[STOP] 所有模块执行后无法确认回到主界面"
        )
        return 2

    if not tasks.handle_main_extras():
        bot.log(
            "[FAIL] 收尾主界面额外任务失败"
        )
        failed_modules.append("final_main_extras")

    if waiting_modules:
        bot.log("[WAITING] 本轮等待后续条件：" + ", ".join(waiting_modules))
    if failed_modules:
        bot.log(
            "[DONE] 全任务流程已完成，但有失败/跳过模块："
            + ", ".join(failed_modules)
        )
        return 2

    bot.log("[DONE] LIVE任务执行完毕")
    return 0


def leave_wechat_to_desktop(bot):
    """Follow the supplied miniapp-home -> WeChat -> white gesture-bar path."""
    frame = bot.frame()
    page, _ = bot.current_page(frame) if frame is not None else ("no_frame", None)
    if page == "main":
        # The supplied game screen has a white circled home control at the
        # upper right. This is a deliberate platform-exit action, not a
        # generic game recovery gesture.
        white_home = bot.vision.match(frame, "wechat_miniapp_home")
        if white_home is None:
            bot.fail_snapshot("wechat_miniapp_home_missing")
            return False
        if not bot.actions.tap_match(white_home, frame.shape,
                                     "微信小程序-右上白色主页控件"):
            return False
        deadline = time.time() + 7
        while time.time() < deadline:
            frame = bot.frame()
            page, _ = bot.current_page(frame) if frame is not None else ("no_frame", None)
            if page in {"wechat_list", "wechat_chat", "wechat_recent"}:
                break
            time.sleep(.35)
        else:
            bot.fail_snapshot("wechat_miniapp_exit_unconfirmed")
            return False
    if page in {"wechat_list", "wechat_chat", "wechat_recent"}:
        if frame is None or not bot.actions.swipe_frame(
                round(frame.shape[1] * .50), round(frame.shape[0] * .988),
                round(frame.shape[1] * .50), round(frame.shape[0] * .35),
                frame.shape, duration_ms=360,
                label="微信底部白色手势条-上滑回桌面"):
            return False
        deadline = time.time() + 6
        while time.time() < deadline:
            frame = bot.frame()
            home = (bot.vision.match(frame, "douyin_home_icon")
                    if frame is not None else None)
            if home:
                bot.log(f"[PLATFORM] 已确认上滑回桌面，抖音图标 "
                        f"score={home.score:.3f}")
                return True
            time.sleep(.35)
        bot.fail_snapshot("wechat_gesture_home_unconfirmed")
        return False
    bot.log(f"[PLATFORM] 微信端结束时页面={page}，不能套用已知退出路径")
    bot.fail_snapshot("wechat_exit_page_unknown")
    return False


def switch_to_platform(bot, platform, desktop_ready=False):
    """Use HOME only for a deliberate app switch, then verify its icon."""
    bot.log(f"[PLATFORM] 准备切换到{platform}端")
    if not desktop_ready and not bot.actions.home():
        bot.log("[PLATFORM] 系统桌面键发送失败")
        return False
    anchor = ("startup_wechat_home_icon" if platform == "wechat"
              else "douyin_home_icon")
    deadline = time.time() + 8
    while time.time() < deadline:
        frame = bot.frame()
        found = bot.vision.match(frame, anchor) if frame is not None else None
        if found:
            bot.log(f"[PLATFORM] 已确认桌面{platform}图标 "
                    f"score={found.score:.3f} frame={found.center}")
            bot.startup_platform = platform
            bot.douyin_stage = ""
            bot.account_records = None
            return True
        time.sleep(.35)
    bot.fail_snapshot(f"{platform}_desktop_icon_missing")
    bot.log(f"[PLATFORM] 未确认桌面{platform}图标；不猜测坐标")
    return False

def main():
    args = parse_args()
    config = load_config()

    bot = None

    try:
        bot = RecoveryBot(
            config,
            dry_run=not args.live,
        )
        bot.startup_platform = ("wechat" if args.platform == "both"
                                else args.platform)
        bot.douyin_stage = ""

        bot.log("=" * 72)
        bot.log("游戏自动化 v1.17")
        bot.log(f"设备：{bot.serial}")
        bot.log(f"启动端：{args.platform}")
        bot.log(
            f"模式：{'LIVE实际点击' if args.live else 'SCAN只识别不点击'}"
        )
        bot.log(
            "启动策略：已知页面自动安全返回；"
            "未知页面停止并截图"
        )
        bot.log("=" * 72)

        bot.log("[INIT] 启动 H.264 实时视频流...")
        bot.stream.start()

        fr = bot.frame()
        bot.log(
            f"[OK] H.264 首帧："
            f"{fr.shape[1]}x{fr.shape[0]}"
        )

        if not args.live:
            return scan(bot)

        bot.runtime_options = prepare_runtime_options(args, args.task)
        bot.capture_target = args.capture
        if bot.runtime_options:
            bot.log(f"[CONFIG] 交互参数：{bot.runtime_options}")
        if args.platform == "both":
            results = {}
            for platform in ("wechat", "douyin"):
                desktop_ready = False
                if platform == "douyin":
                    desktop_ready = leave_wechat_to_desktop(bot)
                    if not desktop_ready:
                        bot.log("[PLATFORM] 微信可见退出路径未确认；"
                                "尝试系统 Home，并重新确认桌面图标")
                if not switch_to_platform(bot, platform, desktop_ready):
                    results[platform] = 2
                    continue
                bot.log(f"[PLATFORM] 开始{platform}端任务")
                try:
                    results[platform] = run_live(bot, args.task)
                except Exception as exc:
                    bot.log(f"[PLATFORM] {platform}端异常："
                            f"{type(exc).__name__}: {exc}")
                    bot.log(traceback.format_exc().rstrip())
                    bot.fail_snapshot(f"{platform}_run_exception")
                    results[platform] = 2
                bot.log(f"[PLATFORM] {platform}端结束 code={results[platform]}")
            bot.log("[PLATFORM] 双端结果：" + ", ".join(
                f"{platform}={results.get(platform, 2)}"
                for platform in ("wechat", "douyin")))
            return 0 if all(results.get(p) == 0 for p in ("wechat", "douyin")) else 2
        return run_live(bot, args.task)

    except KeyboardInterrupt:
        print("\n用户中止。")
        return 130

    except Exception as e:
        print(f"\n[FATAL] {type(e).__name__}: {e}")

        if bot:
            try:
                bot.fail_snapshot("v1_4_fatal")
            except Exception:
                pass

        return 1

    finally:
        if bot:
            bot.close()


if __name__ == "__main__":
    raise SystemExit(main())
