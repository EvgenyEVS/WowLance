"""Профиль фрилансера и каталог найма (BIZ).

Продуктовый контур:

* сервис профиля идемпотентен и заводит портфолио;
* «доступен» без видеопрезентации не сохраняется;
* каталог показывает только verified и только с видео;
* каталог и чужая карточка — инструменты найма: фрилансеру и менеджеру 403;
* правка профиля и портфолио — только своё;
* каналы работы сохраняются как независимые признаки (вход подбора).

Право считается по `User.role`: ни проекта, ни комнаты здесь нет —
BIZ о ROOM не знает (ADR-001, см. `tests_boundaries`).
"""

from django.test import Client, TestCase
from django.urls import reverse

from apps.profiles.models import FreelancerProfile, PortfolioItem
from apps.profiles.services import get_or_create_freelancer_profile
from apps.test_helpers import (
    make_director,
    make_freelancer,
    make_teamlead,
    make_user,
)
from apps.users.models import User

PASSWORD = 'TestPass123!'
VIDEO_URL = 'https://youtube.com/watch?v=test'


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
        # Повторный вызов не дублирует.
        self.assertEqual(get_or_create_freelancer_profile(user).id, profile.id)
        self.assertEqual(FreelancerProfile.objects.filter(user=user).count(), 1)


class ProfileEditTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.freelancer = make_freelancer(email='seller@example.com')
        self.director = make_director(email='dir@example.com')
        self.profile = self.freelancer.freelancer_profile

    def edit_payload(self, **overrides):
        payload = {
            'first_name': 'Селл',
            'last_name': 'Ер',
            'country': 'Россия',
            'level': FreelancerProfile.Level.MIDDLE,
            'experience_years': 3,
            'experience_projects': 10,
            'skills': 'SPIN',
            'key_advantages': 'конверсия 30%',
            'languages': 'Русский: Native',
            'portfolio_links': '',
            'linkedin_url': '',
            'video_url': VIDEO_URL,
            'is_available': 'on',
        }
        payload.update(overrides)
        return payload

    def test_available_profile_requires_video(self):
        """«Доступен» без видеопрезентации форма не пропускает."""
        self.client.force_login(self.freelancer)

        response = self.client.post(
            reverse('profiles:edit'), self.edit_payload(video_url='')
        )

        self.assertEqual(response.status_code, 200)
        form = response.context['form']
        self.assertIn('video_url', form.errors)
        # Форма отклонена целиком — видео в профиле не затёрлось пустым.
        self.profile.refresh_from_db()
        self.assertNotEqual(self.profile.video_url, '')

    def test_profile_edit_is_freelancer_only(self):
        self.client.force_login(self.director)

        response = self.client.get(reverse('profiles:edit'))

        self.assertRedirects(response, reverse('core:home'))

    def test_cannot_delete_others_portfolio_item(self):
        other = make_freelancer(email='other@example.com')
        item = PortfolioItem.objects.create(
            portfolio=other.freelancer_profile.portfolio,
            item_type=PortfolioItem.ItemType.LINK,
            title='Чужое',
            url='https://example.com',
        )
        self.client.force_login(self.freelancer)

        response = self.client.post(
            reverse('profiles:portfolio_delete', kwargs={'item_id': item.id})
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(PortfolioItem.objects.filter(id=item.id).exists())

    def test_both_channel_flags_are_persisted(self):
        """Оба канала — независимые признаки, и оба переживают сохранение.

        Это доменные данные подбора: `staffing.matching` строит на них
        жёсткие фильтры, поэтому значения обязаны храниться раздельно,
        а не выводиться из свободного текста `skills`.

        Пользовательский `ProfileForm` этих полей сейчас не экспонирует.
        """
        self.assertFalse(self.profile.does_cold_calling)
        self.assertFalse(self.profile.does_linkedin_outreach)

        self.profile.does_cold_calling = True
        self.profile.save(update_fields=['does_cold_calling'])
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.does_cold_calling)
        self.assertFalse(self.profile.does_linkedin_outreach)

        self.profile.does_linkedin_outreach = True
        self.profile.save(update_fields=['does_linkedin_outreach'])
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.does_cold_calling)
        self.assertTrue(self.profile.does_linkedin_outreach)


class CatalogFilterTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir-catalog@example.com')

        self.with_video = make_freelancer(
            email='with_video@example.com', first_name='Иван', last_name='СВидео'
        )
        self.set_profile(self.with_video, video_url=VIDEO_URL, is_verified=True)

        self.without_video = make_freelancer(
            email='without_video@example.com', first_name='Петр', last_name='БезВидео'
        )
        self.set_profile(self.without_video, video_url='', is_verified=True)

        self.unverified = make_freelancer(
            email='kirill@example.com', first_name='Кирилл', last_name='НаМодерации'
        )
        self.set_profile(self.unverified, video_url=VIDEO_URL, is_verified=False)

    @staticmethod
    def set_profile(user, *, video_url, is_verified):
        profile = user.freelancer_profile
        profile.video_url = video_url
        profile.is_verified = is_verified
        profile.is_available = True
        profile.save(update_fields=['video_url', 'is_verified', 'is_available'])

    def catalog(self):
        self.client.force_login(self.director)
        response = self.client.get(reverse('profiles:catalog'))
        self.assertEqual(response.status_code, 200)
        return response

    def test_catalog_hides_profiles_without_video(self):
        response = self.catalog()

        self.assertContains(response, 'Иван СВидео')
        self.assertNotContains(response, 'Петр БезВидео')

    def test_catalog_shows_only_verified_profiles(self):
        response = self.catalog()

        self.assertContains(response, 'Иван СВидео')
        self.assertNotContains(response, 'Кирилл НаМодерации')


class CatalogAccessByRoleTests(TestCase):
    """Каталог и чужая карточка — инструменты найма, а не общая витрина."""

    def setUp(self):
        self.client = Client()
        self.director = make_director(email='dir@catalog.test')
        self.teamlead = make_teamlead(email='tl@catalog.test')
        self.admin = make_user(email='adm@catalog.test', role=User.Roles.ADMIN)
        self.manager = make_user(email='mng@catalog.test', role=User.Roles.MANAGER)
        self.freelancer = make_freelancer(email='fr@catalog.test')
        self.other_freelancer = make_freelancer(email='other-fr@catalog.test')

        self.catalog_url = reverse('profiles:catalog')
        self.card_url = reverse(
            'profiles:detail', kwargs={'user_id': self.other_freelancer.id}
        )

    def hiring_roles(self):
        return (
            (self.director, 'director'),
            (self.teamlead, 'teamlead'),
            (self.admin, 'admin'),
        )

    def denied_roles(self):
        return ((self.freelancer, 'freelancer'), (self.manager, 'manager'))

    def test_hiring_roles_open_the_catalog(self):
        for user, label in self.hiring_roles():
            with self.subTest(role=label):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.catalog_url).status_code, 200)

    def test_freelancer_and_manager_get_403_on_the_catalog(self):
        for user, label in self.denied_roles():
            with self.subTest(role=label):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.catalog_url).status_code, 403)

    def test_freelancer_and_manager_get_403_on_a_foreign_card(self):
        for user, label in self.denied_roles():
            with self.subTest(role=label):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.card_url).status_code, 403)

    def test_freelancer_still_opens_his_own_card(self):
        """Своя карточка — не каталог: «Моя карточка» остаётся доступной."""
        self.client.force_login(self.freelancer)
        own_url = reverse('profiles:detail', kwargs={'user_id': self.freelancer.id})

        self.assertEqual(self.client.get(own_url).status_code, 200)

    def test_catalog_back_link_only_for_hiring_roles(self):
        """«К каталогу» — директору на чужой карточке; фрилансеру на своей — нет."""
        own_url = reverse('profiles:detail', kwargs={'user_id': self.freelancer.id})

        with self.subTest(role='freelancer_own'):
            self.client.force_login(self.freelancer)
            response = self.client.get(own_url)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'К каталогу')

        with self.subTest(role='director_foreign'):
            self.client.force_login(self.director)
            response = self.client.get(self.card_url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'К каталогу')

    def test_anonymous_goes_to_login_not_403(self):
        """Поведение для гостя прежнее: логин, а не отказ."""
        response = self.client.get(self.catalog_url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)
