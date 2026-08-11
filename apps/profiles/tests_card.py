from django.test import SimpleTestCase

from apps.profiles.card import highlights_for_profile, rating_stars, seller_title_for_level, video_embed_url


class CardHelpersTests(SimpleTestCase):
    def test_seller_title(self):
        self.assertEqual(seller_title_for_level('junior'), 'Junior seller')
        self.assertEqual(seller_title_for_level('senior'), 'Senior seller')

    def test_rating_stars(self):
        self.assertEqual(rating_stars(4), [True, True, True, True, False])
        self.assertEqual(rating_stars(0), [False, False, False, False, False])
        self.assertEqual(rating_stars(4.6), [True, True, True, True, True])

    def test_video_embed_youtube_and_vimeo(self):
        self.assertEqual(
            video_embed_url('https://www.youtube.com/watch?v=abc123XYZ'),
            'https://www.youtube-nocookie.com/embed/abc123XYZ',
        )
        self.assertEqual(
            video_embed_url('https://vimeo.com/123456789'),
            'https://player.vimeo.com/video/123456789',
        )
        self.assertIsNone(video_embed_url('https://example.com/video.mp4'))

    def test_highlights_prefer_advantages(self):
        class P:
            advantages_list = ['A', 'B', 'C', 'D']
            experience_projects = 10
            acceptance_rate = 0
            country = 'RU'

        self.assertEqual(highlights_for_profile(P()), ['A', 'B', 'C'])
