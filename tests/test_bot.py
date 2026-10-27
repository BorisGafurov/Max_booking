import asyncio
import datetime
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

test_dir = tempfile.mkdtemp()
os.environ["DATA_DIR"] = test_dir
os.environ["DATABASE_URL"] = ""

import database
from database import get_connection, init_db
from handlers.advice import router as advice_router
from handlers.booking import router as booking_router
from handlers.navigation import router as navigation_router
from handlers.referrals import router as referrals_router
from handlers.start import handle_start_logic, router as start_router
from keyboards.advice import (
    get_article_detail_keyboard,
    get_articles_keyboard,
    get_product_detail_keyboard,
    get_products_keyboard,
    get_recommendations_hub_keyboard,
)
from keyboards.booking import (
    get_booking_keyboard,
    get_days_keyboard,
    get_services_keyboard,
    get_slots_keyboard,
    get_sms_keyboard,
)
from keyboards.menu import get_main_menu
from keyboards.navigation import get_navigation_keyboard
from main import fallback_router, setup_bot_commands
from maxapi import Dispatcher
from services.masters import SERVICES, calculate_services
from services.recommendations import get_article, get_articles, get_product, get_products
from services.referrals import (
    get_active_rewards,
    get_pending_referrals,
    get_referral_stats,
    mark_referral_visited,
    mark_reward_used,
    register_referral,
    save_user,
)


class TestMaxBookingBot(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(test_dir, ignore_errors=True)

    def setUp(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM rewards")
            conn.execute("DELETE FROM referrals")
            conn.execute("DELETE FROM users")

    def test_database_and_referral_cycle(self):
        # 1. Save referrer
        referrer_id, is_new1 = save_user(
            max_user_id=1001,
            username="referrer_user",
            first_name="Boris",
            last_name="Gafurov",
        )
        self.assertTrue(is_new1)

        # Update existing user
        ref_id2, is_new1_updated = save_user(
            max_user_id=1001,
            username="referrer_user_updated",
            first_name="Boris",
            last_name="Gafurov",
        )
        self.assertEqual(referrer_id, ref_id2)
        self.assertFalse(is_new1_updated)

        # 2. Save new friend
        friend_id, is_new2 = save_user(
            max_user_id=2002,
            username="friend_user",
            first_name="Alex",
            last_name="Smith",
        )
        self.assertTrue(is_new2)

        # 3. Test self-referral prevention
        success_self, msg_self, _ = register_referral(1001, 1001)
        self.assertFalse(success_self)
        self.assertIn("самого себя", msg_self)

        # 4. Register valid referral
        success_ref, msg_ref, notify_data = register_referral(
            new_user_max_id=2002, referrer_max_id=1001
        )
        self.assertTrue(success_ref)
        self.assertIsNotNone(notify_data)
        self.assertEqual(notify_data["referrer_max_id"], 1001)
        self.assertEqual(notify_data["invited_first_name"], "Alex")

        # 5. Prevent duplicate referral
        success_dup, msg_dup, _ = register_referral(
            new_user_max_id=2002, referrer_max_id=1001
        )
        self.assertFalse(success_dup)

        # 6. Check stats before visit
        stats = get_referral_stats(1001)
        self.assertEqual(stats["started"], 1)
        self.assertEqual(stats["confirmed"], 0)
        self.assertEqual(stats["active_rewards_count"], 0)
        self.assertEqual(stats["active_rewards_sum"], 0)

        # 7. Check pending referrals for admin
        pending_list, total_pending = get_pending_referrals(limit=10, offset=0)
        self.assertGreaterEqual(total_pending, 1)
        ref_entry = [p for p in pending_list if p["invited_max_id"] == 2002][0]

        # 8. Mark visited (confirm referral)
        v_success, v_msg, v_notify = mark_referral_visited(ref_entry["id"])
        self.assertTrue(v_success)
        self.assertEqual(v_notify["referrer_max_id"], 1001)
        self.assertEqual(v_notify["amount"], 300)
        self.assertEqual(v_notify["new_balance"], 300)

        # Try to confirm again (idempotent / should fail)
        v_again, _, _ = mark_referral_visited(ref_entry["id"])
        self.assertFalse(v_again)

        # 9. Check stats after confirmation
        stats_after = get_referral_stats(1001)
        self.assertEqual(stats_after["confirmed"], 1)
        self.assertEqual(stats_after["active_rewards_count"], 1)
        self.assertEqual(stats_after["active_rewards_sum"], 300)

        # 10. Check active rewards for admin
        rewards_list, total_rewards = get_active_rewards(limit=10, offset=0)
        self.assertGreaterEqual(total_rewards, 1)
        reward_entry = [r for r in rewards_list if r["max_user_id"] == 1001][0]

        # 11. Mark reward used
        u_success, u_msg, u_notify = mark_reward_used(reward_entry["id"])
        self.assertTrue(u_success)
        self.assertEqual(u_notify["user_max_id"], 1001)
        self.assertEqual(u_notify["amount"], 300)
        self.assertEqual(u_notify["remaining_balance"], 0)

        # Try to use again
        u_again, _, _ = mark_reward_used(reward_entry["id"])
        self.assertFalse(u_again)

        # 12. Stats after reward used
        stats_final = get_referral_stats(1001)
        self.assertEqual(stats_final["active_rewards_count"], 0)
        self.assertEqual(stats_final["active_rewards_sum"], 0)

    def test_recommendations(self):
        articles = get_articles()
        self.assertIn("styling", articles)
        self.assertIn("beard", articles)

        styling = get_article("styling")
        self.assertIsNotNone(styling)
        self.assertIn("укладывать", styling["title"].lower())

        products = get_products()
        self.assertIn("paste", products)
        self.assertIn("shampoo", products)

        paste = get_product("paste")
        self.assertIsNotNone(paste)
        self.assertTrue(len(paste["links"]) > 0)

    def test_keyboards(self):
        menu_admin = get_main_menu(is_admin=True)
        self.assertIsNotNone(menu_admin)
        self.assertTrue(len(menu_admin.payload.buttons) >= 3)

        menu_user = get_main_menu(is_admin=False)
        self.assertEqual(len(menu_user.payload.buttons), 2)

        booking_kb = get_booking_keyboard()
        self.assertIsNotNone(booking_kb)

        nav_kb = get_navigation_keyboard()
        self.assertIsNotNone(nav_kb)

        hub_kb = get_recommendations_hub_keyboard()
        self.assertIsNotNone(hub_kb)

        art_kb = get_articles_keyboard()
        self.assertIsNotNone(art_kb)

        prod_kb = get_products_keyboard()
        self.assertIsNotNone(prod_kb)

        srv_kb = get_services_keyboard(["hair", "beard"])
        self.assertIsNotNone(srv_kb)

        days_kb = get_days_keyboard({"2026-10-27": {"day_key": "2026-10-27", "title": "Вт 27 окт", "slots": []}})
        self.assertIsNotNone(days_kb)

        slots_kb = get_slots_keyboard([{"time": "11:30", "timestamp": 1793100000}])
        self.assertIsNotNone(slots_kb)

        sms_kb = get_sms_keyboard()
        self.assertIsNotNone(sms_kb)

    def test_masters_service_calculation(self):
        calc_hair = calculate_services(["hair"])
        self.assertEqual(calc_hair["total_price"], 2300)
        self.assertEqual(calc_hair["duration_minutes"], 75)

        calc_both = calculate_services(["hair", "beard"])
        self.assertEqual(calc_both["total_price"], 3000)
        self.assertEqual(calc_both["duration_minutes"], 135)
        self.assertEqual(calc_both["duration_text"], "2 ч 15 мин")

    async def test_dispatcher_and_handlers_integration(self):
        dp = Dispatcher()
        dp.include_routers(
            start_router,
            booking_router,
            navigation_router,
            advice_router,
            referrals_router,
            fallback_router,
        )
        self.assertEqual(len(dp.routers), 6)

        mock_bot = AsyncMock()
        mock_bot.set_commands = AsyncMock()
        await setup_bot_commands(mock_bot)
        mock_bot.set_commands.assert_called_once()

        # Test start logic handler
        mock_user = MagicMock()
        mock_user.user_id = 5555
        mock_user.username = "test_user"
        mock_user.first_name = "Test"
        mock_user.last_name = "User"

        mock_bot.send_message = AsyncMock()
        await handle_start_logic(
            bot=mock_bot,
            user=mock_user,
            chat_id=5555,
            raw_payload="ref_1001",
        )
        mock_bot.send_message.assert_called()

    def test_phone_linking_and_account_merging(self):
        from services.referrals import (
            _save_user_unified,
            get_referral_stats,
            get_user_phone,
            is_phone_number,
            link_user_phone,
            normalize_phone,
            register_referral,
            save_user,
        )

        self.assertTrue(is_phone_number("+79991234567"))
        self.assertTrue(is_phone_number("89991234567"))
        self.assertEqual(normalize_phone("89991234567"), "+79991234567")

        save_user(max_user_id=202, first_name="BorisMAX")
        _save_user_unified(telegram_id=101, first_name="BorisTG")

        save_user(max_user_id=303, first_name="Friend")
        ok, _, _ = register_referral(303, 202)
        self.assertTrue(ok)

        stats_tg = get_referral_stats(101, "telegram")
        stats_max = get_referral_stats(202, "max")
        self.assertEqual(stats_tg["started"], 0)
        self.assertEqual(stats_max["started"], 1)

        link_user_phone(202, "+79991112233", platform="max")
        self.assertEqual(get_user_phone(202, "max"), "+79991112233")

        ok_merge, _ = link_user_phone(101, "89991112233", platform="telegram")
        self.assertTrue(ok_merge)

        stats_tg_after = get_referral_stats(101, "telegram")
        stats_max_after = get_referral_stats(202, "max")
        self.assertEqual(stats_tg_after["started"], 1)
        self.assertEqual(stats_max_after["started"], 1)
        self.assertEqual(stats_tg_after["started"], stats_max_after["started"])

    def test_appointment_lifecycle_and_reminders(self):
        import datetime
        from services.appointments import (
            create_or_update_appointment,
            cancel_appointment,
            get_appointment,
            get_user_upcoming_appointments,
            get_due_2h_reminders,
            mark_reminder_sent,
            get_current_saratov_time,
        )

        now = get_current_saratov_time()
        in_1h = now + datetime.timedelta(hours=1)

        appt_id = create_or_update_appointment(
            phone="+79998887766",
            appointment_time=in_1h,
            services="Мужская стрижка",
            total_price=1200,
            client_name="Клиент Тест",
            platform="max",
            platform_user_id=123456,
        )
        self.assertIsNotNone(appt_id)

        appt = get_appointment(appt_id)
        self.assertIsNotNone(appt)
        self.assertEqual(appt["status"], "confirmed")
        self.assertEqual(appt["client_name"], "Клиент Тест")

        # Due 2h reminders should find this appointment
        due_2h = get_due_2h_reminders()
        self.assertTrue(any(a["id"] == appt_id for a in due_2h))

        # Mark sent
        mark_reminder_sent(appt_id, "2h")
        due_2h_after = get_due_2h_reminders()
        self.assertFalse(any(a["id"] == appt_id for a in due_2h_after))

        # Cancel appointment
        ok, msg, cancelled = cancel_appointment(appt_id)
        self.assertTrue(ok)
        self.assertEqual(cancelled["status"], "cancelled")

        # Upcoming appointments should now be empty
        upcoming = get_user_upcoming_appointments(platform="max", platform_user_id=123456)
        self.assertEqual(len(upcoming), 0)

    def test_masters_session_and_phone_formatting(self):
        from services.masters_sync import (
            format_master_phone,
            get_active_masters_session,
            disconnect_masters_session,
        )
        from database import get_connection

        self.assertEqual(format_master_phone("+79991234567"), "(999) 123-45-67")
        self.assertEqual(format_master_phone("89991234567"), "(999) 123-45-67")

        with get_connection() as conn:
            conn.execute("DELETE FROM masters_session")
            conn.commit()

        self.assertIsNone(get_active_masters_session())

        with get_connection() as conn:
            conn.execute(
                "INSERT INTO masters_session (cookie_phpsessid, master_phone, is_active) VALUES (%s, %s, %s)",
                ("test_session_token", "+79991234567", True if conn.is_pg else 1),
            )
            conn.commit()

        session = get_active_masters_session()
        self.assertIsNotNone(session)
        self.assertEqual(session["master_phone"], "+79991234567")

        disconnect_masters_session()
        self.assertIsNone(get_active_masters_session())

    def test_broadcast_recipients(self):
        from services.referrals import get_broadcast_recipients, save_user
        save_user(max_user_id=777666, first_name="Тест")
        recipients = get_broadcast_recipients("max")
        self.assertIn(777666, recipients)

    def test_client_confirmation_and_followup_reminders(self):
        from services.appointments import (
            create_or_update_appointment,
            confirm_appointment_by_client,
            cancel_appointment,
            get_appointment,
            get_due_confirmation_followup_reminders,
            mark_reminder_sent,
            get_current_saratov_time,
        )

        now = get_current_saratov_time()
        in_day = now + datetime.timedelta(hours=2, minutes=30) if 9 <= now.hour < 21 else now + datetime.timedelta(hours=3)

        appt_id = create_or_update_appointment(
            phone="+79991112233",
            appointment_time=in_day,
            services="Мужская стрижка",
            total_price=1200,
            client_name="Подтверждающий Клиент MAX",
            platform="max",
            platform_user_id=999555,
        )
        self.assertIsNotNone(appt_id)

        appt = get_appointment(appt_id)
        self.assertFalse(bool(appt["client_confirmed"]))

        # Mark evening sent so it can be eligible for morning follow-up
        mark_reminder_sent(appt_id, "evening")

        # If it's daytime (9 <= now.hour < 21), check followup list
        if 9 <= now.hour < 21:
            due = get_due_confirmation_followup_reminders()
            self.assertTrue(any(a["id"] == appt_id for a in due))

        # Client confirms the appointment
        ok, msg, confirmed_appt = confirm_appointment_by_client(appt_id)
        self.assertTrue(ok)
        self.assertEqual(msg, "Запись успешно подтверждена")
        self.assertTrue(bool(confirmed_appt["client_confirmed"]))

        appt_after = get_appointment(appt_id)
        self.assertTrue(bool(appt_after["client_confirmed"]))

        # Once confirmed, it should no longer be in morning followup list
        if 9 <= now.hour < 21:
            due = get_due_confirmation_followup_reminders()
            self.assertFalse(any(a["id"] == appt_id for a in due))

        # Cancel appointment
        ok_cancel, _, cancelled = cancel_appointment(appt_id)
        self.assertTrue(ok_cancel)
        self.assertEqual(cancelled["status"], "cancelled")

        # Trying to confirm a cancelled appointment should fail
        ok_conf_again, msg_conf, _ = confirm_appointment_by_client(appt_id)
        self.assertFalse(ok_conf_again)
        self.assertIn("отменена", msg_conf)

    def test_masters_confirm_and_cancel_sync(self):
        from unittest.mock import MagicMock, patch
        from services.masters_sync import (
            confirm_masters_appointment,
            cancel_masters_appointment,
            disconnect_masters_session,
        )
        from database import get_connection

        # Ensure no active session
        disconnect_masters_session()

        test_appt = {
            "id": 999111,
            "masters_id": "test_m_123",
            "phone": "+79991234567",
            "appointment_time": datetime.datetime.now(),
        }

        # Without active session -> returns False with warning
        ok_c, msg_c = confirm_masters_appointment(test_appt)
        self.assertFalse(ok_c)
        self.assertIn("не подключена", msg_c)

        ok_d, msg_d = cancel_masters_appointment(test_appt)
        self.assertFalse(ok_d)
        self.assertIn("не подключена", msg_d)

        # Now insert active session
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO masters_session (cookie_phpsessid, cookie_csrf, master_phone, is_active) VALUES (%s, %s, %s, %s)",
                ("dummy_sess", "dummy_csrf", "+79991234567", True if conn.is_pg else 1),
            )
            conn.commit()

        # Mock urllib opener to return successful response
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp

        with patch("services.masters_sync._get_masters_opener", return_value=(mock_opener, "dummy_csrf")):
            # Test confirm with active session
            ok_conf, msg_conf = confirm_masters_appointment(test_appt)
            self.assertTrue(ok_conf)
            self.assertIn("успешно подтверждена", msg_conf)

            # Test cancel with active session
            ok_canc, msg_canc = cancel_masters_appointment(test_appt)
            self.assertTrue(ok_canc)
            self.assertIn("успешно отменена", msg_canc)

        # Cleanup session
        disconnect_masters_session()

    def test_master_complete_appointment_auto_rewards_referral(self):
        from services.appointments import complete_appointment, create_manual_appointment, get_appointment
        from services.referrals import get_active_rewards, register_referral, save_user

        # 1. Setup referrer and friend
        ref_id, _ = save_user(max_user_id=8881, first_name="Referrer")
        friend_id, _ = save_user(max_user_id=8882, first_name="Friend")

        reg_ok, reg_msg, _ = register_referral(8882, 8881)
        self.assertTrue(reg_ok)

        # 2. Friend creates an appointment
        appt_time = datetime.datetime.now() + datetime.timedelta(days=1)
        ok_cr, _, appt_id = create_manual_appointment(
            phone="+79170001122",
            appointment_time=appt_time,
            services="Мужская стрижка",
            total_price=1000,
            client_name="Friend Guy",
            platform="max",
        )
        self.assertTrue(ok_cr)
        self.assertIsNotNone(appt_id)

        with get_connection() as conn:
            conn.execute("UPDATE appointments SET user_id = %s WHERE id = %s", (friend_id, appt_id))
            conn.commit()

        # 3. Master completes appointment
        comp_ok, comp_msg, appt_data, ref_notify = complete_appointment(appt_id)
        self.assertTrue(comp_ok)
        self.assertIsNotNone(ref_notify)
        self.assertEqual(ref_notify["amount"], 300)
        self.assertEqual(ref_notify["referrer_max_id"], 8881)

        updated_appt = get_appointment(appt_id)
        self.assertEqual(updated_appt["status"], "completed")

        rewards, total = get_active_rewards()
        self.assertEqual(total, 1)
        self.assertEqual(rewards[0]["user_db_id"], ref_id)
        self.assertEqual(rewards[0]["amount"], 300)

    def test_master_reschedule_appointment(self):
        from services.appointments import create_manual_appointment, get_appointment, reschedule_appointment

        appt_time = datetime.datetime.now() + datetime.timedelta(days=1)
        ok_cr, _, appt_id = create_manual_appointment(
            phone="+79179998877",
            appointment_time=appt_time,
            services="Стрижка бороды",
            total_price=600,
            client_name="Test Resched",
            platform="max",
        )

        new_time = datetime.datetime.now() + datetime.timedelta(days=3, hours=2)
        resched_ok, resched_msg, updated = reschedule_appointment(appt_id, new_time)
        self.assertTrue(resched_ok)

        appt_in_db = get_appointment(appt_id)
        self.assertEqual(appt_in_db["status"], "confirmed")
        self.assertFalse(bool(appt_in_db["reminder_evening_sent"]))
        self.assertFalse(bool(appt_in_db["reminder_2h_sent"]))

    def test_master_schedule_management(self):
        from services.appointments import (
            adjust_master_day_hours,
            get_master_day_schedule,
            get_upcoming_days_schedule,
            set_master_day_schedule,
        )

        tuesday_day = "2026-11-17"  # Tuesday (working in Masters)
        sunday_day = "2026-11-15"   # Sunday (day off)

        # 1. Default schedule from Masters: Tuesday is working, Sunday is day off
        tue_sched = get_master_day_schedule(tuesday_day)
        self.assertTrue(tue_sched["is_working"])
        self.assertEqual(tue_sched["start_time"], "10:00")
        self.assertEqual(tue_sched["end_time"], "19:00")

        sun_sched = get_master_day_schedule(sunday_day)
        self.assertFalse(sun_sched["is_working"])

        # 2. Make Tuesday a day off (override in DB)
        set_master_day_schedule(tuesday_day, is_working=False)
        sched2 = get_master_day_schedule(tuesday_day)
        self.assertFalse(sched2["is_working"])

        # 3. Make working and adjust hours
        set_master_day_schedule(tuesday_day, is_working=True, start_time="09:00", end_time="21:00")
        adj = adjust_master_day_hours(tuesday_day, start_delta_hours=-1, end_delta_hours=1)
        self.assertEqual(adj["start_time"], "08:00")
        self.assertEqual(adj["end_time"], "22:00")

        upcoming = get_upcoming_days_schedule(days_ahead=7)
        self.assertEqual(len(upcoming), 7)

    def test_role_foundation_system(self):
        from services.referrals import assign_user_role, get_user_role, is_admin_user, is_master_user, revoke_user_role

        user_id = 999888
        self.assertFalse(is_admin_user(user_id, platform="max"))
        self.assertFalse(is_master_user(user_id, platform="max"))

        assign_user_role("max", user_id, "master")
        self.assertTrue(is_master_user(user_id, platform="max"))
        self.assertFalse(is_admin_user(user_id, platform="max"))
        self.assertEqual(get_user_role(user_id, platform="max"), "master")

        revoke_user_role("max", user_id)
        self.assertFalse(is_master_user(user_id, platform="max"))

    def test_client_import_and_masters_services(self):
        from services.client_import import (
            get_clients_page,
            get_clients_stats,
            import_clients_from_text,
            update_appointment_client_details,
        )
        from services.masters import get_service_options_for_creation
        from services.appointments import create_or_update_appointment, get_appointment

        # 1. Services
        opts = get_service_options_for_creation()
        names = [o["name"] for o in opts]
        prices = [o["price"] for o in opts]
        self.assertIn("Стрижка (МУЖСКАЯ)", names)
        self.assertIn(2300, prices)
        self.assertNotIn(1000, prices)

        # 2. Text import
        text_data = (
            "Дмитрий Смирнов +79270001122\n"
            "Сергей Попов 89051112233"
        )
        added, updated, msg = import_clients_from_text(text_data)
        self.assertGreaterEqual(added, 2)

        stats = get_clients_stats()
        self.assertGreaterEqual(stats["total"], 2)

        clients, total = get_clients_page(search="Дмитрий")
        self.assertTrue(any("Дмитрий" in (c["first_name"] or "") for c in clients))

        # 3. Create appointment with generic name then update client details
        now = datetime.datetime.now(datetime.timezone.utc)
        appt_id = create_or_update_appointment(
            masters_id="mst_test_max_import_99",
            appointment_time=now + datetime.timedelta(days=1),
            services="Стрижка (МУЖСКАЯ)",
            total_price=2300,
            client_name="Клиент (Masters)",
            phone="+79270001122",
            platform="max",
        )
        ok, umsg = update_appointment_client_details(appt_id, "Дмитрий Смирнов", "+79270001122")
        self.assertTrue(ok)
        appt = get_appointment(appt_id)
        self.assertEqual(appt["client_name"], "Дмитрий Смирнов")

        # 4. Re-sync from masters with placeholder must NOT overwrite custom client name
        create_or_update_appointment(
            masters_id="mst_test_max_import_99",
            appointment_time=now + datetime.timedelta(days=1),
            services="Стрижка (МУЖСКАЯ)",
            total_price=2300,
            client_name="Клиент (Masters)",
            phone="+79173225155",
            platform="max",
        )
        appt_after = get_appointment(appt_id)
        self.assertEqual(appt_after["client_name"], "Дмитрий Смирнов")
        self.assertEqual(appt_after["phone"], "+79270001122")

        # 5. Price update and preservation
        from services.appointments import update_appointment_price_and_service
        ok_p, p_msg, p_appt = update_appointment_price_and_service(appt_id, 700, "Оформление бороды")
        self.assertTrue(ok_p)
        self.assertEqual(p_appt["total_price"], 700)
        self.assertEqual(p_appt["services"], "Оформление бороды")

        # Background masters sync must NOT overwrite this custom price back to 2300
        create_or_update_appointment(
            masters_id="mst_test_max_import_99",
            appointment_time=now + datetime.timedelta(days=1),
            services="Стрижка (МУЖСКАЯ)",
            total_price=2300,
            client_name="Клиент (Masters)",
            phone="+79173225155",
            platform="masters",
        )
        appt_final = get_appointment(appt_id)
        self.assertEqual(appt_final["total_price"], 700)
        self.assertEqual(appt_final["services"], "Оформление бороды")

    def test_link_user_phone_auto_creation_and_master_phone_prevention(self):
        from services.referrals import link_user_phone, get_user_phone
        from services.masters import initiate_booking

        # 1. Non-existent user sends phone -> should auto-create unified user and succeed
        success, msg = link_user_phone(999888, "+7 (999) 111-22-33", platform="max")
        self.assertTrue(success)
        self.assertEqual(get_user_phone(999888, platform="max"), "+79991112233")

        # 2. Master's own phone prevention
        ok, error_msg, session_data = initiate_booking("+79173225155", {"hair"}, int(datetime.datetime.now().timestamp()))
        self.assertFalse(ok)
        self.assertIn("номер самого мастера", error_msg)
        self.assertIsNone(session_data)

    def test_timezone_conversion_precision(self):
        from services.appointments import SARATOV_TZ, parse_to_saratov, create_or_update_appointment, get_appointment
        from zoneinfo import ZoneInfo

        # 1. UTC timestamp: 11:00 UTC = 15:00 Saratov (UTC+4)
        dt_utc = parse_to_saratov("2026-10-31T11:00:00Z")
        self.assertEqual(dt_utc.tzinfo, SARATOV_TZ)
        self.assertEqual(dt_utc.hour, 15)
        self.assertEqual(dt_utc.minute, 0)

        # 2. Moscow timestamp: 14:00 MSK (UTC+3) = 15:00 Saratov (UTC+4)
        dt_msk = parse_to_saratov("2026-10-31T14:00:00+03:00")
        self.assertEqual(dt_msk.tzinfo, SARATOV_TZ)
        self.assertEqual(dt_msk.hour, 15)
        self.assertEqual(dt_msk.minute, 0)

        # 3. Naive timestamp: 15:00 naive = 15:00 Saratov
        dt_naive = parse_to_saratov("2026-10-31 15:00:00")
        self.assertEqual(dt_naive.tzinfo, SARATOV_TZ)
        self.assertEqual(dt_naive.hour, 15)
        self.assertEqual(dt_naive.minute, 0)

        # 4. Save appointment with aware Moscow datetime -> ensure retrieved time is 15:00 Saratov
        moscow_tz = ZoneInfo("Europe/Moscow")
        dt_in_msk = datetime.datetime(2026, 10, 31, 14, 0, 0, tzinfo=moscow_tz)
        appt_id = create_or_update_appointment(
            phone="+79998887766",
            appointment_time=dt_in_msk,
            services="Стрижка",
            total_price=2300,
            client_name="Тест Часового Пояса",
            platform="max",
            masters_id="mst_tz_test_1",
        )
        saved = get_appointment(appt_id)
        saved_dt = parse_to_saratov(saved["appointment_time"])
        self.assertEqual(saved_dt.hour, 15)

    def test_masters_sync_platform_and_user_preservation(self):
        from services.appointments import create_or_update_appointment, get_appointment

        # 1. Book appointment via MAX with user_id=123456
        appt_time = datetime.datetime(2026, 11, 5, 14, 0, 0)
        appt_id = create_or_update_appointment(
            phone="+79271112233",
            appointment_time=appt_time,
            services="Стрижка",
            total_price=2300,
            client_name="Иван Клиент",
            platform="max",
            platform_user_id=123456,
            masters_id="mst_sync_preserve_99",
        )
        first_state = get_appointment(appt_id)
        self.assertEqual(first_state["platform"], "max")
        self.assertEqual(first_state["platform_user_id"], 123456)

        # 2. Background Masters sync runs with platform='masters' and platform_user_id=0
        create_or_update_appointment(
            phone="",
            appointment_time=appt_time,
            services="Стрижка",
            total_price=2300,
            client_name="Клиент (Masters)",
            platform="masters",
            platform_user_id=0,
            masters_id="mst_sync_preserve_99",
        )
        second_state = get_appointment(appt_id)
        # Platform and user_id must remain preserved for MAX reminders!
        self.assertEqual(second_state["platform"], "max")
        self.assertEqual(second_state["platform_user_id"], 123456)
        self.assertEqual(second_state["client_name"], "Иван Клиент")

    def test_user_merge_transfers_appointments_and_roles(self):
        from services.referrals import save_user, link_user_phone
        from services.appointments import create_or_update_appointment, get_appointment

        # Create user A (e.g. registered via MAX without phone)
        user_a_id, _ = save_user(max_user_id=777001, first_name="UserA")
        # User A has an appointment
        appt_id = create_or_update_appointment(
            phone="",
            appointment_time=datetime.datetime(2026, 11, 10, 12, 0, 0),
            services="Стрижка",
            total_price=2300,
            client_name="UserA",
            platform="max",
            platform_user_id=777001,
        )
        # Create user B (has phone from Masters or TG)
        user_b_id, _ = save_user(max_user_id=888002, phone="+79997778899", first_name="UserB")

        # Now User A enters phone +79997778899 -> User A must merge into User B
        ok, msg = link_user_phone(777001, "+79997778899", platform="max")
        self.assertTrue(ok)

        # The appointment must now belong to User B (target_id), not be orphaned!
        appt = get_appointment(appt_id)
        self.assertEqual(appt["user_id"], user_b_id)

    def test_reminder_worker_excludes_master(self):
        from services.reminder_worker import _resolve_platform_user_id

        # 1. Appointment with master phone with spaces/brackets
        appt_master = {
            "id": 1,
            "phone": "+7 (917) 322-51-55",
            "platform": "telegram",
            "platform_user_id": 1010373026,
            "user_id": None,
        }
        self.assertIsNone(_resolve_platform_user_id(appt_master, "telegram"))

        # 2. Appointment with VK master ID
        appt_vk_master = {
            "id": 2,
            "phone": "",
            "platform": "vk",
            "platform_user_id": 90599236,
            "user_id": None,
        }
        self.assertIsNone(_resolve_platform_user_id(appt_vk_master, "vk"))


if __name__ == "__main__":
    unittest.main()


