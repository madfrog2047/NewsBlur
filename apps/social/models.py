import datetime
import zlib
import hashlib
import redis
import mongoengine as mongo
from django.conf import settings
from django.contrib.auth.models import User
from apps.reader.models import UserSubscription
from vendor import facebook
from vendor import tweepy
from utils import log as logging


class MSharedStory(mongo.Document):
    user_id                  = mongo.IntField()
    shared_date              = mongo.DateTimeField()
    comments                 = mongo.StringField()
    has_comments             = mongo.BooleanField(default=False)
    story_feed_id            = mongo.IntField()
    story_date               = mongo.DateTimeField()
    story_title              = mongo.StringField(max_length=1024)
    story_content            = mongo.StringField()
    story_content_z          = mongo.BinaryField()
    story_original_content   = mongo.StringField()
    story_original_content_z = mongo.BinaryField()
    story_content_type       = mongo.StringField(max_length=255)
    story_author_name        = mongo.StringField()
    story_permalink          = mongo.StringField()
    story_guid               = mongo.StringField(unique_with=('user_id',))
    story_tags               = mongo.ListField(mongo.StringField(max_length=250))
    
    meta = {
        'collection': 'shared_stories',
        'indexes': [('user_id', '-shared_date'), ('user_id', 'story_feed_id'), 'story_feed_id'],
        'index_drop_dups': True,
        'ordering': ['shared_date'],
        'allow_inheritance': False,
    }
    
    @property
    def guid_hash(self):
        return hashlib.sha1(self.story_guid).hexdigest()
    
    def save(self, *args, **kwargs):
        if self.story_content:
            self.story_content_z = zlib.compress(self.story_content)
            self.story_content = None
        if self.story_original_content:
            self.story_original_content_z = zlib.compress(self.story_original_content)
            self.story_original_content = None
        
        r = redis.Redis(connection_pool=settings.REDIS_POOL)
        share_key = "S:%s:%s" % (self.story_feed_id, self.guid_hash)
        if self.has_comments:
            r.sadd(share_key, self.user_id)
        else:
            r.srem(share_key, self.user_id)
        
        super(MSharedStory, self).save(*args, **kwargs)
        
        author = MSocialProfile.objects.get(user_id=self.user_id)
        author.count()
    
    def comments_with_author(self, full=False):
        comments = {
            'user_id': self.user_id,
            'comments': self.comments,
            'shared_date': self.shared_date,
        }
        if full:
            author = MSocialProfile.objects.get(user_id=self.user_id)
            comments['author'] = author.to_json()
        return comments
    
    @classmethod
    def stories_with_comments(cls, stories, user):
        r = redis.Redis(connection_pool=settings.REDIS_POOL)
        for story in stories: 
            if story['comment_count']:
                share_key = "S:%s:%s" % (story['story_feed_id'], story['guid_hash'])
                friend_key = "F:%s:F" % (user.pk)
                friends_with_comments = r.sinter(share_key, friend_key)
                if friends_with_comments:
                    params = {
                        'story_guid': story['id'],
                        'story_feed_id': story['story_feed_id'],
                        'user_id__in': friends_with_comments,
                    }
                    shared_stories = cls.objects.filter(**params)
                    story['comments'] = []
                    for shared_story in shared_stories:
                        story['comments'].append(shared_story.comments_with_author())
        return stories
        

class MSocialProfile(mongo.Document):
    user_id              = mongo.IntField()
    username             = mongo.StringField(max_length=30)
    email                = mongo.StringField()
    bio                  = mongo.StringField(max_length=80)
    photo_url            = mongo.StringField()
    photo_service        = mongo.StringField()
    location             = mongo.StringField(max_length=40)
    website              = mongo.StringField(max_length=200)
    subscription_count   = mongo.IntField(default=0)
    shared_stories_count = mongo.IntField(default=0)
    following_count      = mongo.IntField(default=0)
    follower_count       = mongo.IntField(default=0)
    following_user_ids   = mongo.ListField(mongo.IntField())
    follower_user_ids    = mongo.ListField(mongo.IntField())
    unfollowed_user_ids  = mongo.ListField(mongo.IntField())
    
    meta = {
        'collection': 'social_profile',
        'indexes': ['user_id', 'following_user_ids', 'follower_user_ids', 'unfollowed_user_ids'],
        'allow_inheritance': False,
    }
    
    def __unicode__(self):
        return "%s [%s] %s/%s" % (self.username, self.user_id, 
                                  self.subscription_count, self.shared_stories_count)
    
    def save(self, *args, **kwargs):
        if not self.username:
            self.update_user(skip_save=True)
        if not self.subscription_count:
            self.count(skip_save=True)
        super(MSocialProfile, self).save(*args, **kwargs)
        
    @classmethod
    def profiles(cls, user_ids):
        profiles = cls.objects.filter(user_id__in=user_ids)
        return profiles
        
    def to_json(self, full=False):
        params = {
            'user_id': self.user_id,
            'username': self.username,
            'photo_url': self.photo_url,
            'bio': self.bio,
            'location': self.location,
            'website': self.website,
            'subscription_count': self.subscription_count,
            'shared_stories_count': self.shared_stories_count,
            'following_count': self.following_count,
            'follower_count': self.follower_count,
        }
        if full:
            params['photo_service']       = self.photo_service
            params['following_user_ids']  = self.following_user_ids
            params['follower_user_ids']   = self.follower_user_ids
            params['unfollowed_user_ids'] = self.unfollowed_user_ids
        return params
    
    def update_user(self, skip_save=False):
        user = User.objects.get(pk=self.user_id)
        self.username = user.username
        self.email = user.email
        if not skip_save:
            self.save()

    def count(self, skip_save=False):
        self.subscription_count = UserSubscription.objects.filter(user__pk=self.user_id).count()
        self.shared_stories_count = MSharedStory.objects.filter(user_id=self.user_id).count()
        self.following_count = len(self.following_user_ids)
        self.follower_count = len(self.follower_user_ids)
        if not skip_save:
            self.save()
        
    def follow_user(self, user_id, check_unfollowed=False):
        r = redis.Redis(connection_pool=settings.REDIS_POOL)
        
        if check_unfollowed and user_id in self.unfollowed_user_ids:
            return
            
        if user_id not in self.following_user_ids:
            self.following_user_ids.append(user_id)
            if user_id in self.unfollowed_user_ids:
                self.unfollowed_user_ids.remove(user_id)
            self.save()
            
            followee, _ = MSocialProfile.objects.get_or_create(user_id=user_id)
            if self.user_id not in followee.follower_user_ids:
                followee.follower_user_ids.append(self.user_id)
                followee.count()
                followee.save()
        self.count()
        
        following_key = "F:%s:F" % (self.user_id)
        r.sadd(following_key, user_id)
        follower_key = "F:%s:f" % (user_id)
        r.sadd(follower_key, self.user_id)
    
    def unfollow_user(self, user_id):
        r = redis.Redis(connection_pool=settings.REDIS_POOL)
        
        if user_id in self.following_user_ids:
            self.following_user_ids.remove(user_id)
        if user_id not in self.unfollowed_user_ids:
            self.unfollowed_user_ids.append(user_id)
        self.save()
        
        followee = MSocialProfile.objects.get(user_id=user_id)
        if self.user_id in followee.follower_user_ids:
            followee.follower_user_ids.remove(self.user_id)
            followee.count()
            followee.save()
        self.count()
        
        following_key = "F:%s:F" % (self.user_id)
        r.srem(following_key, user_id)
        follower_key = "F:%s:f" % (user_id)
        r.srem(follower_key, self.user_id)

class MSocialServices(mongo.Document):
    user_id               = mongo.IntField()
    autofollow            = mongo.BooleanField(default=True)
    twitter_uid           = mongo.StringField()
    twitter_access_key    = mongo.StringField()
    twitter_access_secret = mongo.StringField()
    twitter_friend_ids    = mongo.ListField(mongo.StringField())
    twitter_picture_url   = mongo.StringField()
    twitter_username      = mongo.StringField()
    twitter_refresh_date  = mongo.DateTimeField()
    facebook_uid          = mongo.StringField()
    facebook_access_token = mongo.StringField()
    facebook_friend_ids   = mongo.ListField(mongo.StringField())
    facebook_picture_url  = mongo.StringField()
    facebook_refresh_date = mongo.DateTimeField()
    upload_picture_url    = mongo.StringField()
    
    meta = {
        'collection': 'social_services',
        'indexes': ['user_id', 'twitter_friend_ids', 'facebook_friend_ids', 'twitter_uid', 'facebook_uid'],
        'allow_inheritance': False,
    }
    
    def __unicode__(self):
        return "%s" % self.user_id
        
    def to_json(self):
        user = User.objects.get(pk=self.user_id)
        return {
            'twitter': {
                'twitter_username': self.twitter_username,
                'twitter_picture_url': self.twitter_picture_url,
                'twitter_uid': self.twitter_uid,
            },
            'facebook': {
                'facebook_uid': self.facebook_uid,
                'facebook_picture_url': self.facebook_picture_url,
            },
            'gravatar': {
                'gravatar_picture_url': "http://www.gravatar.com/avatar/" + \
                                        hashlib.md5(user.email).hexdigest()
            },
            'upload': {
                'upload_picture_url': self.upload_picture_url
            }
        }
    
    def twitter_api(self):
        twitter_consumer_key = settings.TWITTER_CONSUMER_KEY
        twitter_consumer_secret = settings.TWITTER_CONSUMER_SECRET
        auth = tweepy.OAuthHandler(twitter_consumer_key, twitter_consumer_secret)
        auth.set_access_token(self.twitter_access_key, self.twitter_access_secret)
        api = tweepy.API(auth)
        return api
    
    def facebook_api(self):
        graph = facebook.GraphAPI(self.facebook_access_token)
        return graph

    def sync_twitter_friends(self):
        api = self.twitter_api()
        if not api:
            return
            
        friend_ids = list(unicode(friend.id) for friend in tweepy.Cursor(api.friends).items())
        if not friend_ids:
            return
        
        twitter_user = api.me()
        self.twitter_picture_url = twitter_user.profile_image_url
        self.twitter_username = twitter_user.screen_name
        self.twitter_friend_ids = friend_ids
        self.twitter_refreshed_date = datetime.datetime.utcnow()
        self.save()
        
        self.follow_twitter_friends()
        
        profile, _ = MSocialProfile.objects.get_or_create(user_id=self.user_id)
        profile.location = profile.location or twitter_user.location
        profile.bio = profile.bio or twitter_user.description
        profile.website = profile.website or twitter_user.url
        profile.save()
        profile.count()
        if not profile.photo_url or not profile.photo_service:
            self.set_photo('twitter')
        
    def sync_facebook_friends(self):
        graph = self.facebook_api()
        if not graph:
            return

        friends = graph.get_connections("me", "friends")
        if not friends:
            return

        facebook_friend_ids = [unicode(friend["id"]) for friend in friends["data"]]
        self.facebook_friend_ids = facebook_friend_ids
        self.facebook_refresh_date = datetime.datetime.utcnow()
        self.facebook_picture_url = "//graph.facebook.com/%s/picture" % self.facebook_uid
        self.save()
        
        self.follow_facebook_friends()
        
        facebook_user = graph.request('me', args={'fields':'website,bio,location'})
        profile, _ = MSocialProfile.objects.get_or_create(user_id=self.user_id)
        profile.location = profile.location or (facebook_user.get('location') and facebook_user['location']['name'])
        profile.bio = profile.bio or facebook_user.get('bio')
        profile.website = profile.website or facebook_user.get('website')
        profile.save()
        profile.count()
        if not profile.photo_url or not profile.photo_service:
            self.set_photo('facebook')
        
    def follow_twitter_friends(self):
        social_profile, _ = MSocialProfile.objects.get_or_create(user_id=self.user_id)
        following = []
        followers = 0
        
        if self.autofollow:
            # Follow any friends already on NewsBlur
            for twitter_uid in self.twitter_friend_ids:
                user_social_services = MSocialServices.objects.filter(twitter_uid=twitter_uid)
                if user_social_services:
                    followee_user_id = user_social_services[0].user_id
                    social_profile.follow_user(followee_user_id)
                    following.append(followee_user_id)
        
            # Follow any friends already on NewsBlur
            following_users = MSocialServices.objects.filter(twitter_friend_ids__contains=self.twitter_uid)
            for following_user in following_users:
                if following_user.autofollow:
                    following_user_profile = MSocialProfile.objects.get(user_id=following_user.user_id)
                    following_user_profile.follow_user(self.user_id, check_unfollowed=True)
                    followers += 1
        
        user = User.objects.get(pk=self.user_id)
        logging.user(user, "~BB~FRTwitter import: following ~SB%s~SN with ~SB%s~SN followers" % (following, followers))
        
        return following
        
    def follow_facebook_friends(self):
        social_profile, _ = MSocialProfile.objects.get_or_create(user_id=self.user_id)
        following = []
        followers = 0
        
        if self.autofollow:
            # Follow any friends already on NewsBlur
            for facebook_uid in self.facebook_friend_ids:
                user_social_services = MSocialServices.objects.filter(facebook_uid=facebook_uid)
                if user_social_services:
                    followee_user_id = user_social_services[0].user_id
                    social_profile.follow_user(followee_user_id)
                    following.append(followee_user_id)
        
            # Follow any friends already on NewsBlur
            following_users = MSocialServices.objects.filter(facebook_friend_ids__contains=self.facebook_uid)
            for following_user in following_users:
                if following_user.autofollow:
                    following_user_profile = MSocialProfile.objects.get(user_id=following_user.user_id)
                    following_user_profile.follow_user(self.user_id, check_unfollowed=True)
                    followers += 1
        
        user = User.objects.get(pk=self.user_id)
        logging.user(user, "~BB~FRFacebook import: following ~SB%s~SN with ~SB%s~SN followers" % (len(following), followers))
        
        return following
        
    def disconnect_twitter(self):
        self.twitter_uid = None
        self.save()
        
    def disconnect_facebook(self):
        self.facebook_uid = None
        self.save()
        
    def set_photo(self, service):
        profile = MSocialProfile.objects.get(user_id=self.user_id)
        profile.photo_service = service
        if service == 'twitter':
            profile.photo_url = self.twitter_picture_url
        elif service == 'facebook':
            profile.photo_url = self.facebook_picture_url
        elif service == 'upload':
            profile.photo_url = self.upload_picture_url
        elif service == 'gravatar':
            user = User.objects.get(pk=self.user_id)
            profile.photo_url = "http://www.gravatar.com/avatar/" + \
                                hashlib.md5(user.email).hexdigest()
        profile.save()
