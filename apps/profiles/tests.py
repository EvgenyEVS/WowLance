from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.profiles.models import FreelancerProfile, PortfolioItem
from apps.profiles.services import get_or_create_freelancer_profile
from apps.test_helpers import make_director, make_freelancer, make_user
from apps.users.models import User


class ProfileServiceTests(TestCase):
    def test_get_or_create_creates_profile_and_portfolio(self):
        user = make_user(
            email='f1@example.com',
            role=User.Roles.FREELANCER,
            status=User.Status.ACTIVE,
        )
        profile = get_or_create_freelancer_profile(user)
        self.assertEqual(profile.user_id, user.id)
        self.assertTrue(hasattr(profile, 'portfolio'))
        # повторный вызов не дублирует
        profile2 = get_or_create_freelancer_profile(user)
        self.assertEqual(profile.id, profile2.id)
        self.assertEqual(FreelancerProfile.objects.filter(user=user).count(), 1)


class ProfileViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.freelancer = make_freelancer(email='seller@example.com')
        self.director = make_director(email='dir@example.com')
        self.profile = self.freelancer.freelancer_profile

    def test_catalog_requires_login(self):
        response = self.client.get(reverse('profiles:catalog'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_catalog_lists_active_freelancers(self):
        self.client.login(username='dir@example.com', password='TestPass123!')
        response = self.client.get(reverse('profiles:catalog'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.freelancer.full_name)

    def test_catalog_filter_by_level(self):
        self.profile.level = FreelancerProfile.Level.SENIOR
        self.profile.save(update_fields=['level'])
        self.freelancer.first_name = 'Сеньор'
        self.freelancer.last_name = 'Продавец'
        self.freelancer.save(update_fields=['first_name', 'last_name'])

        junior = make_freelancer(
            email='junior@example.com',
            first_name='Джун',
            last_name='Новичок',
        )
        junior.freelancer_profile.level = FreelancerProfile.Level.JUNIOR
        junior.freelancer_profile.save(update_fields=['level'])

        self.client.login(username='dir@example.com', password='TestPass123!')
        response = self.client.get(reverse('profiles:catalog'), {'level': 'senior'})
        self.assertContains(response, 'Сеньор Продавец')
        self.assertNotContains(response, 'Джун Новичок')

    def test_profile_detail(self):
        self.client.login(username='dir@example.com', password='TestPass123!')
        self.profile.country = 'Россия'
        self.profile.level = FreelancerProfile.Level.JUNIOR
        self.profile.rating = 4
        self.profile.key_advantages = [
            '100 клиентов за 7 дней',
            '20 проектов',
            'Диплом Гарварда',
        ]
        self.profile.skills = ['SPIN', 'Cold calls']
        self.profile.linkedin_url = 'https://linkedin.com/in/demo'
        self.profile.video_url = 'https://youtube.com/watch?v=dQw4w9WgXcQ'
        self.profile.save()
        response = self.client.get(
            reverse('profiles:detail', kwargs={'user_id': self.freelancer.id}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.freelancer.first_name)
        self.assertContains(response, 'Junior seller')
        self.assertContains(response, '100 клиентов за 7 дней')
        self.assertContains(response, 'Video presentation 40 sec.')
        self.assertContains(response, 'Проекты:')
        self.assertContains(response, 'Навыки:')
        self.assertContains(response, 'SPIN')
        self.assertContains(response, 'linkedin.com/in/demo')
        self.assertContains(response, 'youtube-nocookie.com/embed/')

    def test_profile_edit_only_freelancer(self):
        self.client.login(username='dir@example.com', password='TestPass123!')
        response = self.client.get(reverse('profiles:edit'))
        self.assertRedirects(response, reverse('core:home'))

    def test_profile_edit_updates_skills(self):
        self.client.login(username='seller@example.com', password='TestPass123!')
        response = self.client.post(
            reverse('profiles:edit'),
            {
                'first_name': 'Селл',
                'last_name': 'Ер',
                'country': 'Россия',
                'level': FreelancerProfile.Level.MIDDLE,
                'experience_years': 3,
                'experience_projects': 10,
                'skills': 'SPIN\nХолодные звонки',
                'key_advantages': 'конверсия 30%',
                'languages': 'Русский: Native',
                'portfolio_links': 'https://example.com/case',
                'linkedin_url': '',
                'video_url': '',
                'is_available': 'on',
            },
        )
        self.assertRedirects(
            response,
            reverse('profiles:detail', kwargs={'user_id': self.freelancer.id}),
        )
        self.profile.refresh_from_db()
        self.freelancer.refresh_from_db()
        self.assertEqual(self.freelancer.first_name, 'Селл')
        self.assertEqual(self.profile.skills, ['SPIN', 'Холодные звонки'])
        self.assertEqual(self.profile.key_advantages, ['конверсия 30%'])
        self.assertEqual(self.profile.languages[0]['language'], 'Русский')

    def test_portfolio_upload_and_delete_requires_post(self):
        self.client.login(username='seller@example.com', password='TestPass123!')
        upload = self.client.post(
            reverse('profiles:portfolio_upload'),
            {
                'title': 'Кейс PDF',
                'file': SimpleUploadedFile(
                    'case.pdf',
                    b'%PDF-1.4 test',
                    content_type='application/pdf',
                ),
            },
        )
        self.assertRedirects(
            upload,
            reverse('profiles:portfolio', kwargs={'user_id': self.freelancer.id}),
        )
        item = PortfolioItem.objects.get(title='Кейс PDF')

        # GET удаление запрещено
        get_delete = self.client.get(
            reverse('profiles:portfolio_delete', kwargs={'item_id': item.id}),
        )
        self.assertEqual(get_delete.status_code, 405)
        self.assertTrue(PortfolioItem.objects.filter(id=item.id).exists())

        post_delete = self.client.post(
            reverse('profiles:portfolio_delete', kwargs={'item_id': item.id}),
        )
        self.assertRedirects(
            post_delete,
            reverse('profiles:portfolio', kwargs={'user_id': self.freelancer.id}),
        )
        self.assertFalse(PortfolioItem.objects.filter(id=item.id).exists())

    def test_cannot_delete_others_portfolio_item(self):
        other = make_freelancer(email='other@example.com')
        item = PortfolioItem.objects.create(
            portfolio=other.freelancer_profile.portfolio,
            item_type=PortfolioItem.ItemType.LINK,
            title='Чужое',
            url='https://example.com',
        )
        self.client.login(username='seller@example.com', password='TestPass123!')
        response = self.client.post(
            reverse('profiles:portfolio_delete', kwargs={'item_id': item.id}),
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(PortfolioItem.objects.filter(id=item.id).exists())
