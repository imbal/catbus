from catbus import client, server

import sys
from datetime import datetime, timezone

def make_server():
    n = server.Registry(name="test")
    @n.add()
    def echo(x):
        return x

    @n.add()
    @server.rpc(safe=True)
    def test():
        return echo

    @n.add()
    class MyEndpoint(server.Service):
        # no self, all methods exposed.
        def which_demo(name):
            if name =='one':
                return MyEndpoint.demo

        class Two(server.Service):
            def test():
                return "nice"

            @server.waiter()
            def expensive(value):
                return server.Waiter(value=value,count=3)

            @expensive.ready()
            def expensive(value, count):
                if count > 0:
                    return server.Waiter(value=value, count=count-1)
                else:
                    return value


        def demo():
            return "A nice demo"

        def rpc_one(a,b):
            return a+b

        def rpc_two(a,b):
            return a*b

        def rpc_three():
            return None

        def now():
            return datetime.now(timezone.utc)


    # A singleton object

    @n.add()
    class Total(server.Singleton):
        def __init__(self):
            self.sum = 0

        def add(self, n):
            self.sum += int(n)
            return self.sum
        
        @server.rpc(safe=True)
        def total(self):
            return self.sum

   # A collection of instances

    jobs = {}

    @n.add()
    class Job():
        Handler = server.Collection.dict_handler('name', jobs)

        def __init__(self, name):
            self.name = name
            self.state = 'run'

        @server.rpc()
        def stop(self):
            self.state = 'stop'

        @server.rpc()
        def start(self):
            self.state = 'run'

        def hidden(self):
            return 'Not exposed over RPC'

        @server.waiter()
        def wait(self):
            return server.Waiter(count=1)

        @wait.ready()
        def wait(self, count):
            if count < 1:
                return self.name
            else:
                return server.Waiter(count=count -1)
            
    return server.Server(n.app(), port=8888)

def run():
    server_thread = make_server()
    server_thread.start()

    print("Running on ",server_thread.url)

    try:
        while True: pass
    finally:
        server_thread.stop()

def test():
    server_thread = make_server()
    server_thread.start()

    print("Running on ",server_thread.url)

    try:
        s= client.Get(server_thread.url+"/test/")
        print(s)
        print(s.echo)

        r = client.Call(s.echo(1))
        print(r)

        test = client.Call(s.test())
        print(test)
        
        x = client.Call(test(x=1))
        print(x)


        print(s.MyEndpoint())
        e = client.Get(s.MyEndpoint())

        demo = client.Call(e.which_demo("one"))
        print('it',demo)

        print(client.Call(demo()))
        print(client.Call(e.rpc_one(1,2)))

        print(client.Call(e.rpc_two(3,b=4)))
        
        print(client.Call(e.rpc_three()))

        print(client.Call(e.now()))

        total = client.Call(s.Total())

        print(client.Call(total.total()))

        client.Call(total.add(5))
        client.Call(total.add(5))
        client.Call(total.add(5))

        print(client.Call(total.total()))


        job = client.Create(s.Job,value=dict(name="butt"))
            # client.Call(s.Job.create(...))

        print(job, job.url, job.actions.keys(), job.attributes)

        for j in client.List(s.Job):
            print(j)
    
        waiter = client.Call(j.wait())
        print(waiter)
        value = client.Wait(waiter, poll_seconds=0.1)
        print(value)

        print(client.Delete(job))

        two = client.Get(e.Two())
        
        exp = client.Call(two.expensive(123))

        exp = client.Wait(exp, poll_seconds=0.5)
        
        print(exp)
    finally:
        server_thread.stop()



if __name__ == '__main__':
    if 'run' in sys.argv[1:]:
        run()
    else:
        test()
