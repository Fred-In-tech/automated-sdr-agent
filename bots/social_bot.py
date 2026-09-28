"""Social post drafter. Content comes from [social] in config/profile.toml.

Note: this does NOT post to X/Twitter — it queues drafts in the database for
you to post yourself. No engagement numbers are invented.
"""

import json
import random
from datetime import datetime, timedelta, timezone
from core.db import get_connection, log_event, init_db
from core.notifications import NotificationManager
from core.config import load_env_file, load_profile, render, template_values


class SocialBot:
    def __init__(self, profile: dict | None = None):
        init_db()
        load_env_file()
        self.profile = profile or load_profile()
        self.social = self.profile.get("social", {})
        self.notifier = NotificationManager()

    def generate_and_schedule_thread(self, topic: str = None) -> dict:
        topics = self.social.get("topics") or ["{{product_name}}"]
        posts = self.social.get("posts") or []
        if not posts:
            return {"status": "skipped", "message": "No [social] posts in config/profile.toml"}

        values = template_values(self.profile)
        topic = topic or render(random.choice(topics), values)
        content = render(random.choice(posts), values)

        conn = get_connection()
        cursor = conn.cursor()

        created_at = datetime.now(timezone.utc).isoformat()
        scheduled_time = (datetime.now(timezone.utc) + timedelta(minutes=random.randint(15, 120))).isoformat()

        cursor.execute("""
            INSERT INTO social_posts (platform, topic, content, scheduled_at, status, likes, retweets, created_at)
            VALUES ('X/Twitter', ?, ?, ?, 'scheduled', 0, 0, ?)
        """, (topic, content, scheduled_time, created_at))

        post_id = cursor.lastrowid
        conn.commit()
        conn.close()

        product_name = self.profile["sender"]["product_name"]
        log_event("SocialBot", "SchedulePost", "success", f"Drafted {product_name} post ID {post_id} on topic '{topic}'.")

        self.notifier.notify_all(
            f"📱 {product_name} social post drafted",
            f"Topic: *{topic}*\nPlatform: X/Twitter\nSuggested time: `{scheduled_time}`"
        )

        return {
            "post_id": post_id,
            "topic": topic,
            "content": content,
            "scheduled_at": scheduled_time,
            "status": "scheduled"
        }

    def publish_pending_posts(self) -> dict:
        """Marks queued drafts as ready. Posting to X itself is manual."""
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT id, topic FROM social_posts WHERE status = 'scheduled'")
        pending_posts = [dict(r) for r in cursor.fetchall()]

        for post in pending_posts:
            cursor.execute("UPDATE social_posts SET status = 'published' WHERE id = ?", (post["id"],))

        conn.commit()
        conn.close()

        summary = f"Marked {len(pending_posts)} social drafts as ready to post."
        log_event("SocialBot", "PublishPosts", "success", summary)

        return {
            "published_count": len(pending_posts),
            "posts": pending_posts
        }

if __name__ == "__main__":
    bot = SocialBot()
    scheduled = bot.generate_and_schedule_thread()
    print("Scheduled:", json.dumps(scheduled, indent=2))
    published = bot.publish_pending_posts()
    print("Published:", json.dumps(published, indent=2))
