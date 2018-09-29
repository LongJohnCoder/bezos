# Reads db logs and visualizes on visdom

import time
import numpy as np
import subprocess
import os.path
from visdom import Visdom
import signal
import logging
import argparse

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.animation as animation

from jinja2 import Template
from utils import dblogging
from utils.misc import human_format
matplotlib.use("agg")


logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(message)s')  # include timestamp
parser = argparse.ArgumentParser(description='Dashboard')
parser.add_argument('-e', '--env', metavar='ENV',
                    help='environment to run visualization')
parser.add_argument('--dbdir', metavar='DBDir',
                    help='db dir, for local case dblogs')
parser.add_argument('--heavy-ids', nargs='+', type=int, default=[],
                    help='idds of heavy rendering')

parser.add_argument('--max-events', type=int, default=10000000,
                    help='max number of event to read from each db')

parser.add_argument('-n', '--env-count', type=int, default=2,
                    help='number of last db logs to read')

parser.add_argument('-s', '--max-steps', type=int, default=100000000,
                    help='max step counts to plot')


class Mytemplates:
    List = Template('''
          <ul>
            {% for n in xs %}
                <li><strong>{{n}}</strong></li>
            {% endfor %}
        </ul>
            ''')

    Videos_bytes = Template('''
            {% for data in xs %}
                <video controls width="{{width}}" height="{{height}}">
                    <source type="video/{{ext}}" src="data:video/{{ext}};base64,{{data}}">
                    Try Firefox or Chrome
                </video>

            {% endfor %}
        </ul>
        ''')

    Videos = Template('''
            {% for path in xs %}
                <video controls width="{{width}}" height="{{height}}">
                    <source src="static/{{path}}" type="video/{{ext}}" >
                    Try Firefox or Chrome
                </video>

            {% endfor %}
        </ul>
        ''')


def _plot_args(data, cache,  viz, wins):
    def get_pr(item):
        k, v = item
        pr = {'source_url': 1, 'env_name': 0}
        if k in pr:
            return pr[k]
        else:
            return 100
    arglist = sorted(data['args'].items(), key=get_pr)

    xs = []
    for k, v in arglist:
        if k not in ['temp_dir', 'tboard_log_dir', 'db_path']:  # we can filter out some keys
            kk, vv = str(k), str(v)
            if kk == 'source_url':
                vv = '<a href="{}">code</a>'.format(vv)
            xs.append(kk + ' : ' + vv)
    viz.text(Mytemplates.List.render(xs=xs), wins['runinfo'],
             opts={'title': 'Arguments Info'})


def _update_line(x, y, viz, wins, title, legend, opts=None):
    ''' Updates or creates a line plot and appends x, y point '''
    xx = np.array([x])
    yy = np.array([y])
    if not title in wins:
        if not opts:
            opts = {'title': title, 'markersize': 1, 'legend': [legend]}
        win = viz.line(X=xx, Y=yy, opts=opts)
        wins[title] = win
    else:
        viz.updateTrace(X=xx, Y=yy, win=wins[title], name=legend)


def _update_bar(xs, viz, wins, rownames, title, legend=None, opts=None):
    if not opts:
        rownames = list(rownames)
        if len(xs.shape) == 1:
            opts = {'rownames': rownames, 'stacked': False, 'title': title}
        else:
            opts = {'rownames': rownames, 'legend': legend,
                    'stacked': True, 'title': title}
    if not title in wins:
        win = viz.bar(X=xs, opts=opts)
        wins[title] = win
    else:
        viz.bar(X=xs, win=wins[title], opts=opts)


def _update_scatter(x, y, viz, wins, title, legend, opts=None):
    if not title in wins:
        if not opts:
            opts = {'title': title, 'legend': [legend]}
        xx = np.array([x, y]).reshape(1, 2)
        win = viz.scatter(X=xx, opts=opts)
        wins[title] = win
    else:
        xx = np.array([x])
        yy = np.array([y])
        viz.updateTrace(X=xx, Y=yy, win=wins[title], name=legend)


def _plot_simple_test(data, cache, viz, wins):
    # ==============Updating individual win ===========
    steps = data['glsteps']
    _update_line(steps, data['avgscore'], viz, wins,
                 title='Average Score', legend=viz.env)
    if 'avgentropy' in data:
        _update_line(steps, data['avgentropy'], viz, wins,
                     title='Average Entropy', legend=viz.env)


def render_agent_video(data, cache):
    ''' renders an agent video and retunrs a path to it '''
    video_name = 'agent-{}.mp4'.format(data['glsteps'])
    video_path = os.path.join(cache, video_name)
    if os.path.isfile(video_path):
        # return cached version
        return video_path

    writer = animation.writers['ffmpeg']
    writer = writer(fps=15, metadata=dict(artist='me'), bitrate=1800)

    state_frames = np.moveaxis(data['states'], 1, 3).squeeze()
    randconv_frames = data['randomconv']
    predvalues = data['predvalues'].squeeze()
    action_distr = data['action_distr']

    # create figs axis and some fine tuning
    fig, ((ax4, ax2), (ax3, ax1)) = plt.subplots(2, 2, figsize=(6, 6), dpi=80)
    fig.subplots_adjust(left=0.1, bottom=0.1, right=0.9,
                        top=0.9, wspace=0.01, hspace=0.01)
    plt.setp(ax1.get_xticklabels(), visible=False)
    plt.setp(ax1.get_yticklabels(), visible=False)
    plt.setp(ax2.get_xticklabels(), visible=False)
    plt.setp(ax4.get_yticklabels(), visible=False)
    # ax2.yaxis.tick_right()
    # ax1.grid(False)
    # ax2.grid(False)
    ax2.set_ylim([min(predvalues)-0.2, max(predvalues)])
    ax3.set_ylim(0, 1)

    # animate things
    def update(num, frames, convs, predvalues, rects, convimg,
               stateimg, predline, action_distr):
        # update observation images plot 1
        stateimg.set_array(frames[num])
        # update random conv visualization
        convimg.set_array(convs[num])
        # update predicted value estimates plot 2
        hist = min(num, 50)
        predline.set_data(np.linspace(0, 1, hist), predvalues[num-hist:num])
        # update  action distribution plot 3
        for rect, h in zip(rects, action_distr[num]):
            rect.set_height(h)

        return (stateimg, convimg, predline)
    predline = matplotlib.lines.Line2D([], [], color='red')
    ax2.add_line(predline)
    stateimg = ax1.imshow(state_frames[0], animated=True)
    convimg = ax4.imshow(randconv_frames[0], animated=True, cmap='gray')
    num_actions = action_distr.shape[1]
    rects = ax3.bar(range(num_actions), [0]*num_actions)  # align='center'

    TO_RENDER = min(800, state_frames.shape[0])
    ani = animation.FuncAnimation(fig, update, TO_RENDER,
                                  fargs=(state_frames, randconv_frames, predvalues, rects,
                                         stateimg, convimg, predline,
                                         action_distr), interval=50, blit=True)

    # conver to video
    time_start_render = time.time()
    state_video_tag = ani.to_html5_video(width=242, height=274)
    logging.info('Rendering time {}'.format(time.time() - time_start_render))
    plt.close()

    ani.save(video_path, writer=writer)
    return video_path


def render_real_video(data, cache):
    video_name = 'real-{}.mp4'.format(data['glsteps'])
    video_path = os.path.join(cache, video_name)
    if os.path.isfile(video_path):
        return video_path

    with open(video_path, 'wb') as f:
        f.write(data['video'])
    #real_video = base64.b64encode(data.video).decode('utf8')
    # real_video_tag = Mytemplates.Videos.render(xs=[real_video], ext='mp4',
    #        width=242, height=274)
    return video_path


def _plot_heavy_test(data, cache, viz, wins, heavy_ids):
    logging.info('Started heavy plot')
    step = human_format(data['glsteps'])
    video_title = 'Step: {}, Score: {} ID: {}'.format(
        step, data['score'], data['idd'])
    # viz.video(videofile=data.video, ispath=False, extension='mp4',
    #        opts={'title':video_title})

    # Get real video coming from gym monitor
    #real_video = base64.b64encode(data.video).decode('utf8')
    real_video = render_real_video(data, cache)
    real_video_tag = Mytemplates.Videos.render(xs=[real_video], ext='mp4',
                                               width=242, height=274)

    if data['idd'] in heavy_ids:
        agent_video = render_agent_video(data, cache)
        agent_video_tag = Mytemplates.Videos.render(xs=[agent_video], ext='mp4',
                                                    width=242, height=274)
    else:
        agent_video_tag = ''

    viz.text(real_video_tag + agent_video_tag, opts={'title': video_title})
    #viz.text(state_video_tag, opts={'title':video_title})
    print('Done heavy plot')


class Dashboard:
    '''Builds LIVE dashboard of visdom based on sqlite log files
       instruction: Run visdom server and then run this script.
       Protocol V1 of dblogger
    '''

    def __init__(self, dbdir, envname, args, names=[], cachedir='cache',
                 interval=1):
        '''
        dbdir: specifies where to look for sqlite log files,
        env_name: name of the environment i.e. Pong-v0 all of them.
        runnames: list of runnnames i.e. nod-0804-0558
        cachedir: dir to cache renered videos, etc..
        interval: time interval to update dashboard

        NOTE, if you want to use caching make symlink of cache in
        visdom/static directory
        '''
        self.dbdir = dbdir
        self.runlist = []
        self.interval = interval
        self.args = args

        # go through each requested env folder, find all sqlite files, take last one
        if len(names) == 0:
            # find all sqlite file in env_name and add names of last 2 of them
            tmp = []
            envdbdir = os.path.join(dbdir, envname)
            for name in os.listdir(envdbdir):
                dbpath = os.path.join(dbdir, envname, name)
                if name.endswith(".sqlite3"):
                    without_ext = os.path.splitext(name)[0]
                    tmp.append((os.path.getctime(dbpath), without_ext))
            names = [x[1] for x in sorted(tmp, reverse=True)[:args.env_count]]

        for name in names:
            dbpath = os.path.join(dbdir, envname, name + '.sqlite3')
            cachepath = os.path.join(cachedir, envname, name)
            #cachepath = os.path.abspath(cachepath)
            if not os.path.exists(cachepath):
                os.makedirs(cachepath)
            self.runlist.append((name, dbpath, cachepath))

        logging.info('Detected following db logs')
        for name, dbpath, cachepath in self.runlist:
            logging.info('name : {}, path: {}'.format(name, dbpath))
        logging.info('=============================')

    def _plot_main(self, env_datas, mainviz, mainwins):
        ''' updates main window 
            env_datas: list of env_name, data pairs'''
        if len(env_datas) == 0:
            return

        # unfortunatelly no update trace for barplot we should keep it ourselves

        for envname, data in env_datas:
            #import ipdb; ipdb.set_trace()
            if data['evtname'] == 'SimpleTest':
                steps = data['glsteps']
                _update_line(steps, data['avgscore'], mainviz, mainwins,
                             title='Average Score', legend=envname)
                _update_line(steps, data['stdscore'], mainviz, mainwins,
                             title='Average Std', legend=envname)
                _update_line(steps, data['avglength'], mainviz, mainwins,
                             title='Average Game Length', legend=envname)
                if 'avgentropy' in data:
                    _update_line(steps, data['avgentropy'], mainviz, mainwins,
                                 title='Average Entropy', legend=envname)
                # _update_line(steps, steps / data['tpassed'], mainviz, mainwins,
                #        title='Steps / Second', legend=envname)
                self.speed_bars[envname] = (steps / data['tpassed'])
            elif data['evtname'] == 'HeavyTest':
                self.action_distr_bars[envname] = data['action_distr']
                eplength = data['action_distr'].shape[0]
                score = data['score']
                _update_scatter(eplength, score, mainviz, mainwins,
                                title='Length vs Score', legend=envname)
            else:
                pass

        if len(self.speed_bars) > 1:
            rownames, xx = zip(*self.speed_bars.items())
            xx = np.array(xx)
            _update_bar(xx, mainviz, mainwins, rownames, title='Steps/S')

        if len(self.action_distr_bars) > 1:
            rownames, xx = zip(*self.action_distr_bars.items())
            env_num = len(rownames)
            action_num = xx[0].shape[1]
            legend = self.action_names
            # each  elem in xx is (num_steps X actions_num) dim lets find chosen actions
            # and make array of size env_num x action_num # chosen actions
            # TODO pass chosen actions in data pack, and replace here
            X = np.zeros((env_num, action_num))
            for i, x in enumerate(xx):
                chosen_actions = np.argmax(x, axis=1)
                for act in chosen_actions:
                    X[i, act] += 1
            # normalize
            #import ipdb; ipdb.set_trace()
            denom = X.sum(axis=1) / 100
            X = X / np.expand_dims(denom, 1)
            #import ipdb; ipdb.set_trace()
            _update_bar(X, mainviz, mainwins, rownames,
                        title='Used Actions', legend=legend)

    def _update_env(self, data, cache, viz, wins, heavy_ids):
        ''' update visdom for specific env,

            viz: visdom env
            cache: directory path to save videos
            windows: dict of windows on this env

        '''
        evtname = data['evtname']
        if evtname == 'ExperimentArgs':
            _plot_args(data, cache, viz, wins)
            self.experiment_args = data
            if 'action_names' in data:
                self.action_names = data['action_names']
            else:
                import gym
                gym_env = gym.make(data['args']['env_name'])
                gym_env.reset()
                if hasattr(gym_env.env, 'get_action_meanings'):
                    self.action_names = gym_env.env.get_action_meanings()
                gym_env.close()
        elif evtname == 'SimpleTest':
            _plot_simple_test(data, cache, viz, wins)
        elif evtname == 'HeavyTest':
            _plot_heavy_test(data, cache, viz, wins, heavy_ids)
        else:
            logging.warning(
                'Unknown tuple instance {}'.format(type(data).__name__))

    def update_envs(self):
        ''' update all visdom envs '''
        #pool = multiprocessing.Pool(3)
        #pool.starmap(_update_env, self.tabs)
        # self.tabs contains db, cache, viz wins
        updated = False
        env_datas = []  # pairs of env and data
        for db, cache, viz, wins in self.tabs:
            try:
                idd, evtname, data, timestamp = next(db)
                if 'glsteps' in data and data['glsteps'] >= args.max_steps:
                    continue
                data['idd'] = idd
                self._update_env(data, cache, viz, wins, self.args.heavy_ids)
                env_datas.append((viz.env, data))
                updated = True
            except StopIteration:
                pass

        # now update main
        # time.sleep(0.2)
        mainviz, mainwins = self.mainviz, self.mainwins
        self._plot_main(env_datas, mainviz, mainwins)

        return updated

    def start(self):
        # each tab corrresponds to separate log
        self.tabs = []

        # shared tab for different logs
        self.mainviz = Visdom(env='main')
        self.mainwins = {}          # name, win pairs

        self.action_names = []

        self.speed_bars = {}
        self.action_distr_bars = {}

        for (runname, dbpath, cachepath) in self.runlist:
            db = dblogging.DBReader(dbpath)
            viz = Visdom(env=runname)

            # setup windows in the env
            wins = {'runinfo': viz.text('info')}

            self.tabs.append((db, cachepath, viz, wins))

        for i in range(self.args.max_events):
            updated = self.update_envs()
            if not updated:
                time.sleep(self.interval)

        print('Log replay Finished')
        time.sleep(1000000)


if __name__ == '__main__':
    args = parser.parse_args()

    def preexec_function():
        # Ignore the SIGINT signal by setting the handler to the standard
        # signal handler SIG_IGN.
        # and attach session id to parent process
        #  signal.signal(signal.SIGINT, signal.SIG_IGN)
        os.setsid()
    try:
        # run visdom as a subprocess,
        # should be carefull not to be left in the wild
        prog = subprocess.Popen('python -m visdom.server', shell=True, preexec_fn=preexec_function,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

        dashboard = Dashboard(args.dbdir, args.env, args=args)
        dashboard.start()
    except KeyboardInterrupt:
        print('keyInterrupted')
    finally:
        os.killpg(os.getpgid(prog.pid), signal.SIGTERM)
        # sys.exit(0)
