"""catbus.client

This is a simple client with a handful of crud 
or http-like methods. The real magic is in the
RemoteObject/RemoteFunction wrapper objects.

"""


import os
import sys
import time

from urllib.parse import urljoin

import requests

from . import dom

HEADERS={'Content-Type': dom.CONTENT_TYPE}

def unwrap_request(method, request, data=None):
    if isinstance(request, dom.Request):
        if data is not None:
            raise Exception('too much data')
        return request

    if hasattr(request, 'url'):
        request = request.url

    return dom.Request(method, request, {}, {}, data)

class Navigable:
    def display(self):
        return self
    def perform(self, action):
        attr = getattr(self, action.path)
        verb = action.verb
        if verb is None:
            if action.arguments:
                verb = 'call'
            elif getattr(attr, 'arguments', None):
                raise Exception('Missing args {}'.format(attr.arguments))
            else:
                verb = 'get'

        if verb == "get":
            return attr()
        elif verb in ('post', 'call'):
            return attr(**dict(action.arguments))
            
        

class CachedResult(Navigable):
    def __init__(self, result):
        self.result = result
        self.url = "<cached>"

class Client:
    def __init__(self):
        self.session=requests.session()

    def Get(self, request, key=None):
        if isinstance(request, CachedResult):
            return request.result
        
        if key and isinstance(request, RemoteDataset):
            request = request.lookup(key)
        elif key:
            raise Exception('first argument not a dataset/collection')
        else:
            request = unwrap_request('GET', request)

        if isinstance(request, CachedResult):
            return request.result

        if request.method != 'GET':
            raise Exception(request.method)

        return self.fetch(request)

    def Set(self, request, key=None, value=None):
        raise Exception('no')
    
    def Create(self, request, key=None, value=None):
        if not key and isinstance(request, RemoteDataset):
            request = request.create(**value)
        else:
            request = unwrap_request('PUT', request, value)
        if request.method not in ('PUT', 'POST'):
            raise Exception('mismatch')

        return self.fetch(request)

    def Update(self, request, key=None, value=None):
        raise Exception('unimplemented')

    def Delete(self, request, key=None, where=None):
        if key and where:
            raise Exception('too many argments')

        if key and isinstance(request, RemoteDataset):
            request = request.delete(key)
        elif isinstance(request, RemoteDataset):
            request = request.delete_list(where=where)
        else:
            request = unwrap_request('DELETE', request)
        if request.method not in ('DELETE', 'POST'):
            raise Exception('mismatch')

        return self.fetch(request)

    def List(self, request, where=None, batch=None):
        if isinstance(request, RemoteDataset):
            request = request.list(where=where, batch=batch)
        elif instance(obj, dom.Request):
            pass
        else:
            raise Exception('no')

        # while ... keep returning them
        obj = self.fetch(request)
        if isinstance(obj, RemoteCursor):
            while obj:
                for x in obj.values():
                    yield x
                request = obj.next(batch)
                if request:
                    obj = self.fetch(request)
                else:
                    obj = None
        else:
            for x in obj:
                yield x
    
    def Call(self, request, method=None, data=None):
        if isinstance(request, CachedResult):
            return request.result
        if isinstance(request, RemoteFunction):
            if method is None:
                if data:
                    request = request(**data)
                else:
                    request = request()
            else:
                raise Exception('no')
        elif isinstance(request, RemoteObject):
            if method is None:
                raise Exception('no')
            else:
                request = getattr(request, method)(**data)
        else:
            request = unwrap_request('POST', request, data)

        if isinstance(request, CachedResult):
            return request.result

        return self.fetch(request)

    def Wait(self, request, poll_seconds=2):
        if isinstance(request, RemoteWaiter):
            request = request()
        else:
            request = unwrap_request('GET', request)

        if request.method != 'GET':
            raise Exception('mismatch')

        obj = self.fetch(request)
        while isinstance(obj, RemoteWaiter):
            wait = obj.metadata.get('wait_seconds', poll_seconds)
            wait  = max(poll_seconds, wait)
            time.sleep(wait) # fixme
            request = obj()
            obj = self.fetch(request)
        return obj



    def Watch(self, request):
        raise Exception('no')

    def Post(self, request, data=None):
        request = unwrap_request('POST', request, data)

        if request.method != 'POST':
            raise Exception('mismatch')
        
        return self.fetch(request)

    def fetch(self, request):
        headers = dict(HEADERS)
        if request.headers:
            headers.update(request.headers)
        
        method = request.method
        url = request.url
        params = request.params
        
        if request.data is not None:
            data = dom.dump(request.data)
        else:
            data = None

        # print('DEBUG', 'Fetching', url)

        result = self.session.request(
                method, 
                url, 
                params=params, 
                headers=headers, 
                data=data
        )

        if result.status_code == 204:
            return None

        def transform(obj):
            if not isinstance(obj, dom.Hyperlink):
                return obj

            if isinstance(obj, dom.Cursor):
                return RemoteCursor(obj.kind, result.url, obj)

            url = urljoin(result.url, obj.url)

            if isinstance(obj, dom.Link):
                return RemoteFunction('GET', url, [])
            if isinstance(obj, dom.Form):
                return RemoteFunction('POST', url, obj.arguments, defaults=obj.defaults)
            if isinstance(obj, dom.Dataset):
                return RemoteDataset(obj.kind, url, obj)
            if isinstance(obj, dom.Resource):
                return RemoteObject(obj.kind, url, obj)
            if isinstance(obj, dom.Namespace):
                return RemoteObject(obj.kind, url, obj)
            if isinstance(obj, dom.Waiter):
                return RemoteWaiter(obj, url) 

            return obj

        #print(result.text)
        #print()
        obj = dom.parse(result.text, transform)

        return obj

class RemoteWaiter(Navigable):
    def __init__(self, obj, url):
        self.url = url
        self.obj = obj

    @property
    def metadata(self):
        return self.obj.metadata

    def __str__(self):
        return "<Waiting for {}>".format(self.url)

    def __call__(self, *args, **kwargs):
        return dom.Request('GET', self.url, {}, {}, None)

class RemoteFunction(Navigable):
    def __init__(self, method, url, arguments, defaults=(), cached=None):
        self.method = method
        self.url = url
        self.arguments = arguments
        self.defaults = defaults
        self.cached = cached

    def __str__(self):
        return "<Link to {}>".format(self.url)

    def __call__(self, *args, **kwargs):
        if self.method == 'GET':
            if self.cached:
                return CachedResult(self.cached)
            return dom.Request('GET', self.url, {}, {}, None)

        data = dict()
        for key, value in zip(self.arguments, args):
            data[key] = value
            if key in kwargs:
                raise Exception('invalid')
        data.update(kwargs)
        for key in self.arguments:
            if key not in data:
                if key in self.defaults:
                    data[key] = self.defaults
                else:
                    raise Exception('missing arg: {}'.format(key))
        return dom.Request('POST', self.url, {}, {}, data)

class RemoteDataset(Navigable):
    def __init__(self, kind, url, obj, selectors=()):
        self.kind = kind
        self.url = url
        self.obj = obj
        self.selectors = selectors

    def __str__(self):
        return "<Dataset to {}>".format(self.url)

    def __getitem__(self, name):
        return self.lookup(name)

    def __call__(self, *args, **kwargs):
        return self.create(*args, **kwargs)

    def lookup(self, name):
        url = "{}/id/{}".format(self.url, name)
        return dom.Request('GET', url, {}, {}, None)
    
    def create(self, *args, **kwargs):
        url = "{}/new".format(self.url)
        arguments = self.obj.metadata['new']
        data = dict()
        for key, value in zip(arguments, args):
            data[key] = value
            if key in kwargs:
                raise Exception('invalid')
        data.update(kwargs)
        return dom.Request('POST', url, {}, {}, data)

    def delete(self, name):
        url = "{}/id/{}".format(self.url, name)
        return dom.Request('DELETE', url, {}, {}, None)

    def get_params(self, selector, batch):
        params = dict()
        if selector and self.selectors:
            raise Exception('no')
        if selector:
            params['where'] = selector
        if self.selectors: 
            params['where'] = dom.dump_selector(self.selectors)
        if batch:
            params['limit'] = batch
        return params

    def delete_list(self, where=None):
        url = "{}/list".format(self.url)
        params = self.get_params(where, None)
        if 'where' not in params:
            raise Exception('missing where')
        return dom.Request('DELETE', url, params, {}, None)

    def list(self, where=None, batch=None):
        url = "{}/list".format(self.url)
        params = self.get_params(where, batch)
        return dom.Request('GET', url, params, {}, None)

    def next(self, batch=None):
        # so that remote collection / selectors have
        # similar apis
        return self.list(batch)

    def where(self, **kwargs):
        new_selectors = []
        new_selectors.extend(self.selectors)
        names = self.obj.metadata['list']
        
        for name, value in kwargs.items():
            if name not in names:
                raise Exception('no')
            new_selectors.append(dom.Operator.Equals(
                key=name,
                value=value,
            ))

        return RemoteDataset(self.kind, self.url, self.obj, new_selectors)

    def not_where(self, **kwargs):
        new_selectors = []
        new_selectors.extend(self.selectors)
        names = self.obj.metadata['list']

        for name, value in kwargs.items():
            if name not in names:
                raise Exception('no')
            new_selectors.append(dom.Operator.NotEquals(
                key=name,
                value=value
        ))
        
        return RemoteDataset(self.kind, self.url, self.obj, new_selectors)


class RemoteCursor(Navigable):
    def __init__(self,kind, base_url, obj):
        self.base_url = base_url
        self.kind = kind
        self.obj = obj

    def next(self, batch):
        if self.obj.metadata['continue']:
            params = dict()
            url = urljoin(self.base_url, self.obj.metadata['collection'])
            #url = "{}/list".format(url)
            params['selector'] = self.obj.metadata['selector']
            params['continue'] = self.obj.metadata['continue']
            if batch:
                params['limit'] = batch

            return dom.Request('GET', url, params, {}, None)

    def values(self):
        return self.obj.items

    # __getitem__
    # length
    # iter
    # contains
    # next()

class RemoteObject(Navigable):
    def __init__(self,kind, url, obj):
        self.kind = kind
        self.url = url
        self.obj = obj
        self.links = obj.metadata.get('links', [])
        self.attributes = getattr(obj, 'attributes', {})
        self.actions = obj.metadata.get('actions', {})
        self.embeds = obj.metadata.get('embeds', {})

    def __str__(self):
        return "<{} at {}>".format(self.kind, self.url)

    def display(self):
        return """
    url: {}
    links: {}
    actions: {}
    embeds: {}
    attributes: {}
""".format(self.url,
        ", ".join(self.links),
        ", ".join(self.actions.keys()),
        ", ".join("{!r}:{!r}".format(k,v) for k,v in self.embeds.items()),
        ", ".join("{!r}:{!r}".format(k,v) for k,v in self.attributes.items()),
    )

    def __getattr__(self, name):
        if name in self.attributes:
            return self.attributes[name]
        
        if '?' in self.url:
            url, params = self.url.split('?',1)
            url = '{}/{}?{}'.format(url, name, params)
        else:
            url = '{}/{}'.format(self.url, name)

        if self.links and name in self.links:
            return RemoteFunction('GET', url, (), cached=self.embeds.get(name))
        elif self.actions:
            arguments = self.actions[name]
            if isinstance(arguments, Navigable):
                return arguments
            elif isinstance(arguments, (tuple, list)):
                return RemoteFunction('POST', url, arguments)
        raise AttributeError('no')

client = Client()

Get = client.Get
Set = client.Set
Create = client.Create
Update = client.Update
Delete = client.Delete
List = client.List
Call = client.Call
Wait = client.Wait
Watch = client.Watch
Post = client.Post
