"""
The MIT License (MIT)

Copyright (c) 2019-2020 MrDandycorn

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

import asyncio
import sys
import traceback
import textwrap
import aiohttp
import enum
import os
from random import getrandbits
from collections.abc import Iterable
from io import BytesIO

from vk_botting.user import get_blocked_user, get_unblocked_user, User
from vk_botting.group import get_post, get_board_comment, get_market_comment, get_photo_comment, get_video_comment, get_wall_comment, get_deleted_photo_comment,\
    get_deleted_video_comment, get_deleted_board_comment, get_deleted_market_comment, get_deleted_wall_comment, get_officers_edit, get_poll_vote, Group
from vk_botting.attachments import get_photo, get_video, get_audio
from vk_botting.message import Message, UserMessage
from vk_botting.attachments import get_attachment, get_user_attachments, DocType, Attachment, AttachmentType
from vk_botting.states import get_state
from vk_botting.exceptions import VKApiError, LoginError, VKException
from vk_botting.general import convert_params


class UserMessageFlags(enum.IntFlag):
    Unread = 1,
    Outbox = 2,
    Replied = 4,
    Important = 8,
    Chat = 16,
    Friends = 32,
    Spam = 64,
    Deleted = 128,
    Fixed = 256,
    Media = 512,
    Hidden = 65536,
    DeleteForAll = 131072,
    NotDelivered = 262144


class _ClientEventTask(asyncio.Task):
    def __init__(self, original_coro, event_name, coro, *, loop):
        super().__init__(coro, loop=loop)
        self.__event_name = event_name
        self.__original_coro = original_coro

    def __repr__(self):
        info = [
            ('state', self._state.lower()),
            ('event', self.__event_name),
            ('coro', repr(self.__original_coro)),
        ]
        if self._exception is not None:
            info.append(('exception', repr(self._exception)))
        return '<ClientEventTask {}>'.format(' '.join('%s=%s' % t for t in info))


class Client:
    """Class that represent Client for interation with VK Api

    .. warning::

        Should not be used outside of :class:`.Bot`, as it is not intended and will probably not work

    """

    def __init__(self, **kwargs):
        self.v = kwargs.get('v', '5.999')
        self.force = kwargs.get('force', False)
        self.loop = asyncio.get_event_loop()
        self.group = None
        self.user = None
        self.key = None
        self.server = None
        self._listeners = {}
        timeout = aiohttp.ClientTimeout(total=100, connect=10)
        user_agent = kwargs.get('user_agent', None)
        if user_agent:
            headers = {
                'User-Agent': user_agent
            }
            self.session = kwargs.get('session', aiohttp.ClientSession(timeout=timeout, headers=headers))
        else:
            self.session = kwargs.get('session', aiohttp.ClientSession(timeout=timeout))
        self._implemented_events = ['message_new', 'message_reply', 'message_allow', 'message_deny', 'message_edit', 'message_typing_state', 'photo_new', 'audio_new', 'video_new', 'wall_reply_new', 'wall_reply_edit', 'wall_reply_delete', 'wall_reply_restore', 'wall_post_new', 'wall_repost', 'board_post_new', 'board_post_edit', 'board_post_restore', 'board_post_delete', 'photo_comment_new', 'photo_comment_edit', 'photo_comment_delete', 'photo_comment_restore', 'video_comment_new', 'video_comment_edit', 'video_comment_delete', 'video_comment_restore', 'market_comment_new', 'market_comment_edit', 'market_comment_delete', 'market_comment_restore', 'poll_vote_new', 'group_join', 'group_leave', 'group_change_settings', 'group_change_photo', 'group_officers_edit', 'user_block', 'user_unblock']
        self.extra_events = []
        self.token = None

    def Payload(self, **kwargs):
        kwargs['access_token'] = self.token
        kwargs['v'] = self.v
        return kwargs

    class botCommandException(Exception):
        pass

    def wait_for(self, event, *, check=None, timeout=None):
        """|coro|

        Waits for an event to be dispatched.

        This could be used to wait for a user to reply to a message or to edit a message in a self-containedway.

        The ``timeout`` parameter is passed onto :func:`asyncio.wait_for`. By default,
        it does not timeout. Note that this does propagate the
        :exc:`asyncio.TimeoutError` for you in case of timeout and is provided for
        ease of use.

        In case the event returns multiple arguments, a :class:`tuple` containing those
        arguments is returned instead. Please check the
        :ref:`documentation <vk_api_events>` for a list of events and their
        parameters.

        This function returns the **first event that meets the requirements**.

        Examples
        ---------
        Waiting for a user reply: ::

            @bot.command()
            async def greet(ctx):
                await ctx.send('Say hello!')
                def check(m):
                    return m.text == 'hello' and m.from_id == ctx.from_id
                msg = await bot.wait_for('message_new', check=check)
                await ctx.send('Hello {.from_id}!'.format(msg))

        Parameters
        ------------
        event: :class:`str`
            The event name, similar to the :ref:`event reference <vk_api_events>`,
            but without the ``on_`` prefix, to wait for.
        check: Optional[Callable[..., :class:`bool`]]
            A predicate to check what to wait for. The arguments must meet the
            parameters of the event being waited for.
        timeout: Optional[:class:`float`]
            The number of seconds to wait before timing out and raising
            :exc:`asyncio.TimeoutError`.

        Raises
        -------
        asyncio.TimeoutError
            If a timeout is provided and it was reached.

        Returns
        --------
        Any
            Returns no arguments, a single argument, or a :class:`tuple` of multiple
            arguments that mirrors the parameters passed in the
            :ref:`event reference <vk_api_events>`.
        """

        future = self.loop.create_future()
        if check is None:
            def _check(*_):
                return True
            check = _check

        ev = event.lower()
        try:
            listeners = self._listeners[ev]
        except KeyError:
            listeners = []
            self._listeners[ev] = listeners

        listeners.append((future, check))
        return asyncio.wait_for(future, timeout, loop=self.loop)

    async def general_request(self, url, post=False, **params):
        params = convert_params(params)
        for tries in range(5):
            try:
                req = self.session.post(url, data=params) if post else self.session.get(url, params=params)
                async with req as r:
                    if r.content_type == 'application/json':
                        return await r.json()
                    return await r.text()
            except Exception as e:
                print('Got exception in request: {}\nRetrying in {} seconds'.format(e, tries*2+1), file=sys.stderr)
                await asyncio.sleep(tries*2+1)

    async def vk_request(self, method, post=True, **kwargs):
        """|coro|

        Implements abstract VK Api method request.

        Parameters
        ----------
        method: :class:`str`
            String representation of method name (e.g. 'users.get')
        post: :class:`bool`
            If request should be POST. Defaults to true. Changing this is not recommended
        kwargs: :class:`dict`
            Payload arguments to send along with request

            .. note::

                access_token and v parameters should not be passed as they are automatically added from current bot attributes

        Returns
        -------
        :class:`dict`
            Dict representation of json response received from the server
        """
        for param in kwargs:
            if isinstance(kwargs[param], (list, tuple)):
                kwargs[param] = ','.join(map(str, kwargs[param]))
        res = await self.general_request('https://api.vk.com/method/{}'.format(method), post=post, **self.Payload(**kwargs))
        error = res.get('error', None)
        if error and error.get('error_code', None) == 6:
            await asyncio.sleep(1)
            return await self.vk_request(method, post=post, **kwargs)
        elif error and error.get('error_code', None) == 10 and 'could not check access_token now' in error.get('error_msg', ''):
            await asyncio.sleep(0.1)
            return await self.vk_request(method, post=post, **kwargs)
        return res

    async def get_users(self, *uids, fields=None, name_case=None):
        """|coro|

        Alias for VK Api 'users.get' method call

        Parameters
        ----------
        uids: List[:class:`int`]
            List of user ids to request
        fields: List[:class:`str`]
            Optional. Fields that should be requested from VK Api. None by default
        name_case: :class:`str`
            Optional. Name case for users' names to be returned in. 'nom' by default. Can be 'nom', 'gen', 'dat', 'acc', 'ins' or 'abl'

        Raises
        --------
        vk_botting.VKApiError
            When error is returned by VK API.

        Returns
        -------
        List[:class:`.User`]
            List of :class:`.User` instances for requested users
        """
        if name_case is None:
            name_case = 'nom'
        users = await self.vk_request('users.get', user_ids=uids, fields=fields, name_case=name_case)
        if 'error' in users.keys():
            raise VKApiError('[{error_code}] {error_msg}'.format(**users['error']))
        users = users.get('response')
        return [User(user) for user in users]

    async def get_groups(self, *gids):
        """|coro|

        Alias for VK Api 'groups.get' method call

        Parameters
        ----------
        gids: List[:class:`int`]
            List of group ids to request

        Raises
        --------
        vk_botting.VKApiError
            When error is returned by VK API.

        Returns
        -------
        List[:class:`.Group`]
            List of :class:`.Group` instances for requested groups
        """
        groups = await self.vk_request('groups.getById', group_ids=','.join(map(str, gids)))
        if 'error' in groups.keys():
            raise VKApiError('[{error_code}] {error_msg}'.format(**groups['error']))
        groups = groups.get('response')
        return [Group(group) for group in groups]

    async def get_pages(self, *ids, fields=None, name_case=None):
        """|coro|

        Gets pages for given ids, whether it is Group or User

        Parameters
        ----------
        ids: List[:class:`int`]
            List of ids to request
        fields: List[:class:`str`]
            Optional. Fields that should be requested from VK Api for users. None by default
        name_case: :class:`str`
            Optional. Name case for users' names to be returned in. 'nom' by default. Can be 'nom', 'gen', 'dat', 'acc', 'ins' or 'abl'

        Raises
        --------
        vk_botting.VKApiError
            When error is returned by VK API.

        Returns
        -------
        List[Union[:class:`.Group`, :class:`.User`]]
            List of :class:`.Group` or :class:`.User` instances for requested ids
        """
        g = []
        u = []
        for pid in ids:
            if pid < 0:
                g.append(-pid)
            else:
                u.append(pid)
        users = await self.get_users(*u, fields=fields, name_case=name_case)
        groups = await self.get_groups(*g)
        res = []
        for pid in ids:
            if pid < 0:
                for group in groups:
                    if -pid == group.id:
                        res.append(group)
                        break
                else:
                    res.append(None)
            else:
                for user in users:
                    if pid == user.id:
                        res.append(user)
                        break
                else:
                    res.append(None)
        return res

    async def get_user(self, uid, fields=None, name_case=None):
        """|coro|

        Alias for VK Api 'users.get' method call that returns only one user.

        Parameters
        ----------
        uid: :class:`int`
            Id of user to request
        fields: List[:class:`str`]
            Optional. Fields that should be requested from VK Api. None by default
        name_case: :class:`str`
            Optional. Name case for user's name to be returned in. 'nom' by default. Can be 'nom', 'gen', 'dat', 'acc', 'ins' or 'abl'

        Raises
        --------
        vk_botting.VKApiError
            When error is returned by VK API.

        Returns
        -------
        :class:`.User`
            :class:`.User` instance for requested user
        """
        user = await self.get_users(uid, fields=fields, name_case=name_case)
        if user:
            return user[0]
        return None

    async def fetch_user(self, *args, **kwargs):
        return await self.get_user(*args, **kwargs)

    async def get_group(self, gid):
        """|coro|

        Alias for VK Api 'groups.get' method call that returns only one group.

        Parameters
        ----------
        gid: :class:`int`
            Id of group to request

        Raises
        --------
        vk_botting.VKApiError
            When error is returned by VK API.

        Returns
        -------
        :class:`.Group`
            :class:`.Group` instance for requested group
        """
        group = await self.get_groups(gid)
        if group:
            return group[0]
        return None

    async def fetch_group(self, *args, **kwargs):
        return await self.get_group(*args, **kwargs)

    async def get_page(self, pid, fields=None, name_case=None):
        """|coro|

        Gets page for given id, whether it is Group or User

        Parameters
        ----------
        pid: :class:`int`
            Id of page to request
        fields: List[:class:`str`]
            Optional. Fields that should be requested from VK Api for users. None by default
        name_case: :class:`str`
            Optional. Name case for user's name to be returned in. 'nom' by default. Can be 'nom', 'gen', 'dat', 'acc', 'ins' or 'abl'

        Raises
        --------
        vk_botting.VKApiError
            When error is returned by VK API.

        Returns
        -------
        Union[:class:`.Group`, :class:`.User`]
            :class:`.Group` or :class:`.User` instance for requested id
        """
        page = await self.get_pages(pid, fields=fields, name_case=name_case)
        if page:
            return page[0]
        return None

    async def fetch_page(self, *args, **kwargs):
        return await self.get_page(*args, **kwargs)

    async def get_own_page(self):
        """|coro|

        Gets page for current token, whether it is Group or User

        Raises
        --------
        vk_botting.VKApiError
            When error is returned by VK API.

        Returns
        -------
        Union[:class:`.Group`, :class:`.User`]
            :class:`.Group` or :class:`.User` instance for current token
        """
        from vk_botting.group import Group
        user = await self.vk_request('users.get')
        if not user.get('response'):
            group = await self.vk_request('groups.getById')
            return Group(group.get('response')[0])
        return User(user.get('response')[0])

    async def upload_document(self, peer_id, file, type=DocType.DOCUMENT, title=None):
        """|coro|

        Upload a document to conversation with given peer_id.

        Returns ready-to-use in send_message attachment.

        Parameters
        ----------
        peer_id: :class:`int`
            Peer_id of the destination. The uploaded document cannot be used outside of given conversation.
        file: :class:`str`
            Path to document to upload. Can be relative.
        type: :class:`str`
            Uploaded document type. Can be value from :class:`.DocType` enum.
        title: :class:`str`
            Title for uploaded document. Filename by default.

        Returns
        -------
        :class:`.Attachment`
            :class:`.Attachment` instance representing uploaded document.
        """
        if isinstance(type, DocType):
            type = type.value
        r = await self.vk_request('docs.getMessagesUploadServer', peer_id=peer_id, type=type)
        imurl = r['response']['upload_url']
        files = {'file': open(file, 'rb')}
        r = await self.session.post(imurl, data=files)
        r = await r.json()
        filedata = r['file']
        if title is None:
            title = os.path.splitext(file)[0]
        r = await self.vk_request('docs.save', file=filedata, title=title)
        doc = r['response']
        doc = doc[doc['type']]
        return Attachment(doc['owner_id'], doc['id'], AttachmentType.DOCUMENT)

    async def upload_photo(self, peer_id, filename=None, url=None, raw=None, format=None):
        """|coro|

        Upload a photo to conversation with given peer_id.

        Returns ready-to-use in send_message attachment.

        Parameters
        ----------
        peer_id: :class:`int`
            Peer_id of the destination. The uploaded photo cannot be used outside of given conversation.
        filename: :class:`str`
            Path to image to upload. Can be relative.
        url: :class:`str`
            Url of image to upload. Should be a direct url to supported image format, otherwise wont work.
        raw: :class:`bytes`
            Raw bytes of image to upload. If used, format has to be provided.
        format: :class:`str`
            Extension of image to upload. Should be used only alongside raw data.

        Returns
        -------
        :class:`.Attachment`
            :class:`.Attachment` instance representing uploaded photo.
        """
        if not (filename or url or raw):
            raise VKException('No image source provided')
        if (filename and url) or (filename and raw) or (url and raw):
            raise VKException('Can only upload one image at a time')
        if raw and not format:
            raise VKException('Format has to be provided when using raw data')
        r = await self.vk_request('photos.getMessagesUploadServer', peer_id=peer_id)
        imurl = r['response']['upload_url']
        if filename:
            files = {'photo': open(filename, 'rb')}
        elif url:
            imbts = await self.session.get(url)
            cnt = imbts.content_type
            if not cnt.startswith('image/'):
                raise TypeError('URL passed does not lead to an image')
            ext = cnt[6:]
            imbts = await imbts.read()
            files = aiohttp.FormData()
            files.add_field('photo', imbts, filename='temp.{}'.format(ext))
        else:
            files = aiohttp.FormData()
            files.add_field('photo', raw, filename='temp.{}'.format(format.lower()))
        r = await self.session.post(imurl, data=files)
        r = await r.json(content_type='text/html')
        r = await self.vk_request('photos.saveMessagesPhoto', **r)
        doc = r['response'][0]
        return Attachment(doc['owner_id'], doc['id'], AttachmentType.PHOTO)

    def build_msg(self, msg):
        """
        Build :class:`.Message` instance from message object :class:`dict`.

        Normally should not be used at all.

        Parameters
        ----------
        msg: :class:`dict`
            :class:`dict` representation of Message object returned by VK API

        Returns
        -------
        :class:`.Message`
            :class:`.Message` instance representing original object
        """
        res = Message(msg)
        if res.attachments:
            for i in range(len(res.attachments)):
                res.attachments[i] = get_attachment(res.attachments[i])
        if res.fwd_messages:
            for i in range(len(res.fwd_messages)):
                res.fwd_messages[i] = self.build_msg(res.fwd_messages[i])
        if res.reply_message:
            res.reply_message = self.build_msg(res.reply_message)
        res.bot = self
        return res

    async def enable_longpoll(self):
        events = dict([(event, 1) for event in self._implemented_events])
        res = await self.vk_request('groups.setLongPollSettings', group_id=self.group.id, enabled=1, api_version='5.103', **events)
        return res

    async def print_warnings(self):
        res = await self.vk_request('groups.getLongPollSettings', group_id=self.group.id)
        events = res.get('response').get('events')
        if all([not events[event] for event in events]):
            print('WARNING:  All longpoll events are disabled. Bot will not function until events are enabled', file=sys.stderr)
        elif not events.get('message_new'):
            print('WARNING:  message_new event is disabled. Commands will not function until message_new is enabled', file=sys.stderr)
        api_ver = res.get('response').get('api_version')
        minor = int(api_ver.split('.')[1])
        if minor < 103:
            print('WARNING:  You are using old LongPoll API version, consider upgrading to newer one', file=sys.stderr)

    async def get_longpoll_server(self):
        res = await self.vk_request('groups.getLongPollServer', group_id=self.group.id)
        error = res.get('error', None)
        if error and error['error_code'] == 100:
            if self.force:
                await self.enable_longpoll()
                return await self.get_longpoll_server()
            raise VKApiError('Longpoll is disabled for this group. Enable longpoll or try force mode')
        elif error:
            raise VKApiError('[{error_code}]{error_msg}'.format(**error))
        self.key = res['response']['key']
        self.server = res['response']['server'].replace(r'\/', '/')
        ts = res['response']['ts']
        return ts

    async def longpoll(self, ts):
        payload = {'key': self.key,
                   'act': 'a_check',
                   'ts': ts,
                   'wait': '10'}
        if not self.is_group:
            payload['mode'] = 10
        try:
            res = await self.general_request(self.server, **payload)
        except asyncio.TimeoutError:
            return ts, []
        if 'ts' not in res.keys() or 'failed' in res.keys():
            ts = await self.get_longpoll_server()
        else:
            ts = res['ts']
        updates = res.get('updates', [])
        return ts, updates

    def handle_message(self, message):
        msg = self.build_msg(message)
        payload = message.get('payload')
        if payload and payload == '{"command":"start"}':
            return self.dispatch('conversation_start', msg)
        action = msg.action
        if action:
            action_type = action.type
            return self.dispatch(action_type, msg)
        return self.dispatch('message_new', msg)

    def handle_message_reply(self, t, obj):
        msg = self.build_msg(obj)
        return self.dispatch(t, msg)

    def handle_message_edit(self, t, obj):
        msg = self.build_msg(obj)
        return self.dispatch(t, msg)

    async def handle_message_typing_state(self, t, obj):
        state = await get_state(obj)
        return self.dispatch(t, state)

    async def handle_message_allow(self, t, obj):
        user = await self.get_pages(obj)
        return self.dispatch(t, user)

    async def handle_photo_new(self, t, obj):
        photo = await get_photo(self, obj)
        return self.dispatch(t, photo)

    async def handle_photo_comment_new(self, t, obj):
        comment = await get_photo_comment(self, obj)
        return self.dispatch(t, comment)

    async def handle_photo_comment_delete(self, t, obj):
        deleted = await get_deleted_photo_comment(self, obj)
        return self.dispatch(t, deleted)

    async def handle_audio_new(self, t, obj):
        audio = await get_audio(self, obj)
        return self.dispatch(t, audio)

    async def handle_video_new(self, t, obj):
        video = await get_video(self, obj)
        return self.dispatch(t, video)

    def handle_video_comment_new(self, t, obj):
        comment = get_video_comment(self, obj)
        return self.dispatch(t, comment)

    async def handle_video_comment_delete(self, t, obj):
        deleted = await get_deleted_video_comment(self, obj)
        return self.dispatch(t, deleted)

    async def handle_wall_post_new(self, t, obj):
        post = await get_post(self, obj)
        return self.dispatch(t, post)

    async def handle_wall_reply_new(self, t, obj):
        comment = await get_wall_comment(self, obj)
        return self.dispatch(t, comment)

    async def handle_wall_reply_delete(self, t, obj):
        deleted = await get_deleted_wall_comment(self, obj)
        return self.dispatch(t, deleted)

    async def handle_board_post_new(self, t, obj):
        comment = await get_board_comment(self, obj)
        return self.dispatch(t, comment)

    async def handle_board_post_delete(self, t, obj):
        deleted = await get_deleted_board_comment(self, obj)
        return self.dispatch(t, deleted)

    async def handle_market_comment_new(self, t, obj):
        comment = await get_market_comment(self, obj)
        return self.dispatch(t, comment)

    async def handle_market_comment_delete(self, t, obj):
        deleted = await get_deleted_market_comment(self, obj)
        return self.dispatch(t, deleted)

    async def handle_group_leave(self, t, obj):
        user = await self.get_pages(obj['user_id'])
        return self.dispatch(t, user)

    async def handle_group_join(self, t, obj):
        user = await self.get_pages(obj['user_id'])
        return self.dispatch(t, user)

    async def handle_user_block(self, t, obj):
        blocked = await get_blocked_user(self, obj)
        return self.dispatch(t, blocked)

    async def handle_user_unblock(self, t, obj):
        unblocked = await get_unblocked_user(self, obj)
        return self.dispatch(t, unblocked)

    async def handle_poll_vote_new(self, t, obj):
        vote = await get_poll_vote(self, obj)
        return self.dispatch(t, vote)

    async def handle_group_officers_edit(self, t, obj):
        edit = await get_officers_edit(self, obj)
        return self.dispatch(t, edit)

    def handle_update(self, update):
        t = update['type']
        if t == 'message_new':
            return self.handle_message(update['object']['message'])
        obj = update['object']
        if t == 'message_reply' and 'on_message_reply' in self.extra_events:
            return self.handle_message_reply(t, obj)
        elif t == 'message_edit' and 'on_message_edit' in self.extra_events:
            return self.handle_message_edit(t, obj)
        elif t == 'message_typing_state' and 'on_message_typing_state' in self.extra_events:
            return self.loop.create_task(self.handle_message_typing_state(t, obj))
        elif t in ['message_allow', 'message_deny'] and any(event in self.extra_events for event in ['on_message_allow', 'on_message_deny']):
            return self.loop.create_task(self.handle_message_allow(t, obj))
        elif t == 'photo_new' and 'on_photo_new' in self.extra_events:
            return self.loop.create_task(self.handle_photo_new(t, obj))
        elif t in ['photo_comment_new', 'photo_comment_edit', 'photo_comment_restore'] and any(
                event in self.extra_events for event in ['on_photo_comment_new', 'on_photo_comment_edit', 'on_photo_comment_restore']):
            return self.loop.create_task(self.handle_photo_comment_new(t, obj))
        elif t == 'photo_comment_delete' and 'on_photo_comment_delete' in self.extra_events:
            return self.loop.create_task(self.handle_photo_comment_delete(t, obj))
        elif t == 'audio_new' and 'on_audio_new' in self.extra_events:
            return self.loop.create_task(self.handle_audio_new(t, obj))
        elif t == 'video_new' and 'on_video_new' in self.extra_events:
            return self.loop.create_task(self.handle_video_new(t, obj))
        elif t in ['video_comment_new', 'video_comment_edit', 'video_comment_restore'] and any(
                event in self.extra_events for event in ['on_video_comment_new', 'on_video_comment_edit', 'on_video_comment_restore']):
            return self.handle_video_comment_new(t, obj)
        elif t == 'video_comment_delete' and 'on_video_comment_delete' in self.extra_events:
            return self.loop.create_task(self.handle_video_comment_delete(t, obj))
        elif t in ['wall_post_new', 'wall_repost'] and any(event in self.extra_events for event in ['on_wall_post_new', 'on_wall_repost']):
            return self.loop.create_task(self.handle_wall_post_new(t, obj))
        elif t in ['wall_reply_new', 'wall_reply_edit', 'wall_reply_restore'] and any(
                event in self.extra_events for event in ['on_wall_reply_new', 'on_wall_reply_edit', 'on_wall_reply_restore']):
            return self.loop.create_task(self.handle_wall_reply_new(t, obj))
        elif t == 'wall_reply_delete' and 'on_wall_reply_delete' in self.extra_events:
            return self.loop.create_task(self.handle_wall_reply_delete(t, obj))
        elif t in ['board_post_new', 'board_post_edit', 'board_post_restore'] and any(
                event in self.extra_events for event in ['on_board_post_new', 'on_board_post_edit', 'on_board_post_restore']):
            return self.loop.create_task(self.handle_board_post_new(t, obj))
        elif t == 'board_post_delete' and 'on_board_post_delete' in self.extra_events:
            return self.loop.create_task(self.handle_board_post_delete(t, obj))
        elif t in ['market_comment_new', 'market_comment_edit', 'market_comment_restore'] and any(
                event in self.extra_events for event in ['on_market_comment_new', 'on_market_comment_edit', 'on_market_comment_restore']):
            return self.loop.create_task(self.handle_market_comment_new(t, obj))
        elif t == 'market_comment_delete' and 'on_market_comment_delete' in self.extra_events:
            return self.loop.create_task(self.handle_market_comment_delete(t, obj))
        elif t == 'group_leave' and 'on_group_leave' in self.extra_events:
            return self.loop.create_task(self.handle_group_leave(t, obj))
        elif t == 'group_join' and 'on_group_join' in self.extra_events:
            return self.loop.create_task(self.handle_group_join(t, obj))
        elif t == 'user_block' and 'on_user_block' in self.extra_events:
            return self.loop.create_task(self.handle_user_block(t, obj))
        elif t == 'user_unblock' and 'on_user_unblock' in self.extra_events:
            return self.loop.create_task(self.handle_user_unblock(t, obj))
        elif t == 'poll_vote_new' and 'on_poll_vote_new' in self.extra_events:
            return self.loop.create_task(self.handle_poll_vote_new(t, obj))
        elif t == 'group_officers_edit' and 'on_group_officers_edit' in self.extra_events:
            return self.loop.create_task(self.handle_group_officers_edit(t, obj))
        elif t not in self._implemented_events and 'on_unknown' in self.extra_events:
            return self.dispatch('unknown', update)

    def dispatch(self, event, *args, **kwargs):
        method = 'on_' + event
        listeners = self._listeners.get(event)
        if listeners:
            removed = []
            for i, (future, condition) in enumerate(listeners):
                if future.cancelled():
                    removed.append(i)
                    continue

                try:
                    result = condition(*args)
                except Exception as exc:
                    future.set_exception(exc)
                    removed.append(i)
                else:
                    if result:
                        if len(args) == 0:
                            future.set_result(None)
                        elif len(args) == 1:
                            future.set_result(args[0])
                        else:
                            future.set_result(args)
                        removed.append(i)

            if len(removed) == len(listeners):
                self._listeners.pop(event)
            else:
                for idx in reversed(removed):
                    del listeners[idx]

        try:
            coro = getattr(self, method)
        except AttributeError:
            pass
        else:
            self._schedule_event(coro, method, *args, **kwargs)

    async def on_error(self, event_method, *args, **kwargs):
        """|coro|

        The default error handler provided by the client.

        By default this prints to :data:`sys.stderr` however it could be
        overridden to have a different implementation.

        Check :func:`vk_botting.on_error` for more details.
        """
        print('Ignoring exception in {}'.format(event_method), file=sys.stderr)
        traceback.print_exc()

    async def _run_event(self, coro, event_name, *args, **kwargs):
        try:
            await coro(*args, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception:
            try:
                await self.on_error(event_name, *args, **kwargs)
            except asyncio.CancelledError:
                pass

    def _schedule_event(self, coro, event_name, *args, **kwargs):
        wrapped = self._run_event(coro, event_name, *args, **kwargs)
        return _ClientEventTask(original_coro=coro, event_name=event_name, coro=wrapped, loop=self.loop)

    async def send_message(self, peer_id=None, message=None, *, attachment=None, sticker_id=None, keyboard=None, reply_to=None, forward_messages=None):
        """|coro|

        Sends a message to the given destination with the text given.

        The content must be a type that can convert to a string through ``str(message)``.

        If the content is set to ``None`` (the default), then the ``attachment`` or ``sticker_id`` parameter must
        be provided.

        If the ``attachment`` parameter is provided, it must be :class:`str`, List[:class:`str`], :class:`.Attachment` or List[:class:`.Attachment`]

        If the ``keyboard`` parameter is provided, it must be :class:`str` or :class:`.Keyboard` (recommended)

        Parameters
        ------------
        peer_id: :class:`int`
            Id of conversation to send message to
        message: :class:`str`
            The text of the message to send.
        attachment: Union[List[:class:`str`], :class:`str`, List[:class:`.Attachment`], :class:`.Attachment`]
            The attachment to the message sent.
        sticker_id: Union[:class:`str`, :class:`int`]
            Sticker_id to be sent.
        keyboard: :class:`.Keyboard`
            The keyboard to send along message.
        reply_to: Union[:class:`str`, :class:`int`]
            A message id to reply to.
        forward_messages: Union[List[:class:`int`], List[:class:`str`]]
            Message ids to be forwarded along with message.

        Raises
        --------
        vk_botting.VKApiError
            When error is returned by VK API.

        Returns
        ---------
        :class:`.Message`
            The message that was sent.
        """
        if isinstance(attachment, str):
            pass
        elif isinstance(attachment, Iterable):
            attachment = ','.join(map(str, attachment))
        else:
            attachment = str(attachment)
        if message:
            message = str(message)
            if len(message) > 4096:
                w = textwrap.TextWrapper(width=4096, replace_whitespace=False)
                messages = w.wrap(message)
                for message in messages[:-1]:
                    await self.send_message(peer_id, message)
                return await self.send_message(peer_id, messages[-1], attachment=attachment, sticker_id=sticker_id, keyboard=keyboard, reply_to=reply_to, forward_messages=forward_messages)
        params = {'group_id': self.group.id, 'random_id': getrandbits(64), 'peer_id': peer_id, 'message': message, 'attachment': attachment,
                  'reply_to': reply_to, 'forward_messages': forward_messages, 'sticker_id': sticker_id, 'keyboard': keyboard}
        res = await self.vk_request('messages.send', **params)
        if 'error' in res.keys():
            if res['error'].get('error_code') == 9:
                await asyncio.sleep(1)
                return await self.send_message(peer_id, message, attachment=attachment, sticker_id=sticker_id,
                                               keyboard=keyboard, reply_to=reply_to, forward_messages=forward_messages)
            raise VKApiError('[{error_code}] {error_msg}'.format(**res['error']))
        params['id'] = res['response']
        params['from_id'] = -self.group.id
        return self.build_msg(params)

    async def _run(self, owner_id):
        if owner_id and owner_id.__class__ is not int:
            raise TypeError('Owner_id must be positive integer, not {0.__class__.__name__}'.format(owner_id))
        if owner_id and owner_id < 0:
            raise VKApiError('Owner_id must be positive integer')
        user = await self.get_own_page()
        if isinstance(user, Group):
            self.is_group = True
            self.group = user
            if self.is_group and owner_id:
                raise VKApiError('Owner_id passed together with group access_token')
            ts = await self.get_longpoll_server()
            await self.print_warnings()
            self.dispatch('ready')
            updates = []
            while True:
                try:
                    lp = self.loop.create_task(self.longpoll(ts))
                    for update in updates:
                        self.handle_update(update)
                    ts, updates = await lp
                except Exception as e:
                    print('Ignoring exception in longpoll cycle:\n{}'.format(e), file=sys.stderr)
                    ts = await self.get_longpoll_server()
        raise LoginError('User token passed to group client')

    def run(self, token, owner_id=None):
        """A blocking call that abstracts away the event loop
        initialisation from you.

        .. warning::
            This function must be the last function to call due to the fact that it
            is blocking. That means that registration of events or anything being
            called after this function call will not execute until it returns.

        Parameters
        ----------
        token: :class:`str`
            Bot token. Should be group token or user token with access to group
        owner_id: :class:`int`
            Should only be passed alongside user token. Owner id of group to connect to
        """
        self.token = token
        self.loop.create_task(self._run(owner_id))
        self.loop.run_forever()


class UserClient(Client):

    def __init__(self, **kwargs):
        user_agent = kwargs.get('user_agent', 'KateMobileAndroid/52.1 lite-445 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)')
        kwargs.setdefault('user_agent', user_agent)
        super().__init__(**kwargs)

    async def build_user_msg(self, msg):
        res = UserMessage(msg)
        if res.attachments:
            res.attachments = await get_user_attachments(res.attachments)
        res.bot = self
        return res

    async def get_user_longpoll(self):
        res = await self.vk_request('messages.getLongPollServer', group_id=self.group.id, lp_version=3)
        error = res.get('error', None)
        if error and error['error_code'] == 15:
            raise LoginError('User has no access to messages API. Try generating token with vk_botting.auth methods')
        elif error:
            raise VKApiError('[{error_code}] {error_msg}'.format(**res['error']))
        self.key = res['response']['key']
        server = res['response']['server'].replace(r'\/', '/')
        self.server = 'https://{}'.format(server)
        ts = res['response']['ts']
        return ts

    async def longpoll(self, ts):
        payload = {'key': self.key,
                   'act': 'a_check',
                   'ts': ts,
                   'wait': '10'}
        if not self.is_group:
            payload['mode'] = 10
        try:
            res = await self.general_request(self.server, **payload)
        except asyncio.TimeoutError:
            return ts, []
        if 'ts' not in res.keys() or 'failed' in res.keys():
            ts = await self.get_user_longpoll()
        else:
            ts = res['ts']
        updates = res.get('updates', [])
        return ts, updates

    async def handle_user_update(self, update):
        t = update.pop(0)
        if t == 4:
            data = {
                'id': update.pop(0),
                'flags': UserMessageFlags(update.pop(0)),
                'peer_id': update.pop(0),
                'date': update.pop(0),
                'text': update.pop(1),
                'attachments': update.pop(1)
            }
            msg = await self.build_user_msg(data)
            return self.dispatch('message_new', msg)
        elif 'on_unknown' in self.extra_events:
            return self.dispatch('unknown', update)

    async def _run(self, owner_id):
        if owner_id and owner_id.__class__ is not int:
            raise TypeError('Owner_id must be positive integer, not {0.__class__.__name__}'.format(owner_id))
        if owner_id and owner_id < 0:
            raise VKApiError('Owner_id must be positive integer')
        user = await self.get_own_page()
        if isinstance(user, User):
            self.is_group = False
            self.group = Group({})
            self.user = user
            ts = await self.get_user_longpoll()
            self.dispatch('ready')
            updates = []
            while True:
                try:
                    lp = self.loop.create_task(self.longpoll(ts))
                    for update in updates:
                        self.loop.create_task(self.handle_user_update(update))
                    ts, updates = await lp
                except Exception as e:
                    print('Ignoring exception in longpoll cycle:\n{}'.format(e), file=sys.stderr)
                    ts = await self.get_user_longpoll()
        raise LoginError('Group token passed to user client')
