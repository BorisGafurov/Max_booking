import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

test_dir = tempfile.mkdtemp()
os.environ["DATA_DIR"] = test_dir

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
from keyboards.booking import get_booking_keyboard
from keyboards.menu import get_main_menu
from keyboards.navigation import get_navigation_keyboard
from main import fallback_router, setup_bot_commands
from maxapi import Dispatcher
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


if __name__ == "__main__":
    unittest.main()
