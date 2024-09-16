import { SimplePool } from 'nostr-tools';
import Geohash from 'latlon-geohash';
import {
  Coordinator,
  type Exchange,
  type Origin,
  type PublicOrder,
  type Settings,
  defaultExchange,
} from '.';
import defaultFederation from '../../static/federation.json';
import currencyDict from '../../static/assets/currencies.json';
import { systemClient } from '../services/System';
import { getHost } from '../utils';
import { coordinatorDefaultValues } from './Coordinator.model';
import { updateExchangeInfo } from './Exchange.model';
import { fromUnixTime } from 'date-fns';

type FederationHooks = 'onFederationUpdate';

export class Federation {
  constructor(origin: Origin, settings: Settings, hostUrl: string) {
    this.coordinators = Object.entries(defaultFederation).reduce(
      (acc: Record<string, Coordinator>, [key, value]: [string, any]) => {
        if (getHost() !== '127.0.0.1:8000' && key === 'local') {
          // Do not add `Local Dev` unless it is running on localhost
          return acc;
        } else {
          acc[key] = new Coordinator(value, origin, settings, hostUrl);

          return acc;
        }
      },
      {},
    );
    this.exchange = {
      ...defaultExchange,
      totalCoordinators: Object.keys(this.coordinators).length,
    };
    this.book = {};
    this.hooks = {
      onFederationUpdate: [],
    };

    Object.keys(defaultFederation).forEach((key) => {
      if (key !== 'local' || getHost() === '127.0.0.1:8000') {
        // Do not add `Local Dev` unless it is running on localhost
        this.addCoordinator(origin, settings, hostUrl, defaultFederation[key]);
      }
    });

    this.exchange.loadingCoordinators = Object.keys(this.coordinators).length;
    this.loading = true;

    const host = getHost();
    const url = `${window.location.protocol}//${host}`;

    const tesnetHost = Object.values(this.coordinators).find((coor) => {
      return Object.values(coor.testnet).includes(url);
    });
    if (tesnetHost) settings.network = 'testnet';
  }

  public coordinators: Record<string, Coordinator>;
  public exchange: Exchange;
  public book: Record<string, PublicOrder>;
  public loading: boolean;

  public hooks: Record<FederationHooks, Array<() => void>>;

  public relayPool: SimplePool = new SimplePool();

  connectNostr = (): void => {
    this.loading = true;
    this.book = {};

    const relays = ['ws://satstraoq35jffvkgpfoqld32nzw2siuvowanruindbfojowpwsjdgad.onion/nostr'];

    this.exchange.loadingCoordinators = relays.length;

    this.relayPool.trustedRelayURLs = new Set<string>(relays);
    this.relayPool.subscribeMany(
      relays,
      [
        {
          authors: Object.values(defaultFederation)
            .map((f) => f.nostrHexPubkey)
            .filter((item) => item !== undefined),
          kinds: [38383],
          '#s': ['pending'],
          '#n': ['mainnet'],
        },
      ],
      {
        onevent: (event) => {
          const publicOrder: PublicOrder = {
            id: 0,
            coordinatorShortAlias: '',
            created_at: new Date(),
            expires_at: new Date(),
            type: 1,
            currency: null,
            amount: '',
            has_range: false,
            min_amount: null,
            max_amount: null,
            payment_method: '',
            is_explicit: false,
            premium: '',
            satoshis: null,
            maker: null,
            escrow_duration: 10800, // FIXME
            bond_size: '',
            latitude: null,
            longitude: null,
            maker_nick: '',
            maker_hash_id: '8a928ef7b30bd07a4bf2493bee55e9b3a00c4962be0a743649b7ac2f0e031c4e', // FIXME
            satoshis_now: null, // FIXME
            price: null, // FIXME
          };
          let dTag = '';
          event.tags.forEach((tag) => {
            switch (tag[0]) {
              case 'd':
                dTag = tag[1];
                break;
              case 'k':
                publicOrder.type = tag[1] === 'sell' ? 1 : 0;
                break;
              case 'expiration':
                publicOrder.expires_at = fromUnixTime(parseInt(tag[1], 10));
                break;
              case 'fa':
                if (tag[2]) {
                  publicOrder.has_range = true;
                  publicOrder.min_amount = tag[1] ?? null;
                  publicOrder.max_amount = tag[2] ?? null;
                } else {
                  publicOrder.amount = tag[1];
                }
                break;
              case 'bond':
                publicOrder.bond_size = tag[1];
                break;
              case 'name':
                publicOrder.maker_nick = tag[1];
                break;
              case 'premium':
                publicOrder.premium = tag[1];
                break;
              case 'pm':
                tag.shift();
                publicOrder.payment_method = tag.join(' ');
                break;
              case 'g':
                const { lat, lon } = Geohash.decode(tag[1]);
                publicOrder.latitude = lat;
                publicOrder.longitude = lon;
                break;
              case 'f':
                const currencyNumber = Object.entries(currencyDict).find(
                  ([_key, value]) => value === tag[1],
                );
                publicOrder.currency = currencyNumber?.[0] ? parseInt(currencyNumber[0], 10) : null;
                break;
              case 'source':
                const orderUrl = tag[1].split('/');
                publicOrder.id = parseInt(orderUrl[orderUrl.length - 1] ?? '0');
                const coordinatorIdentifier = orderUrl[orderUrl.length - 2] ?? '';
                publicOrder.coordinatorShortAlias = Object.entries(defaultFederation).find(
                  ([key, value]) => value.identifier === coordinatorIdentifier,
                )?.[0];
                break;
              default:
                break;
            }
          });

          // price = limitsList[index].price * (1 + premium / 100);

          this.book[dTag] = publicOrder;
        },
        oneose: () => {
          this.exchange.loadingCoordinators = this.exchange.loadingCoordinators - 1;
          this.loading = this.exchange.loadingCoordinators > 0;
          this.updateExchange();
          this.triggerHook('onFederationUpdate');
        },
      },
    );
  };

  addCoordinator = (
    origin: Origin,
    settings: Settings,
    hostUrl: string,
    attributes: Record<any, any>,
  ): void => {
    const value = {
      ...coordinatorDefaultValues,
      ...attributes,
    };
    this.coordinators[value.shortAlias] = new Coordinator(value, origin, settings, hostUrl);
    this.exchange.totalCoordinators = Object.keys(this.coordinators).length;
    this.updateEnabledCoordinators();
    this.triggerHook('onFederationUpdate');
  };

  // Hooks
  registerHook = (hookName: FederationHooks, fn: () => void): void => {
    this.hooks[hookName].push(fn);
  };

  triggerHook = (hookName: FederationHooks): void => {
    this.hooks[hookName]?.forEach((fn) => {
      fn();
    });
  };

  onCoordinatorSaved = (): void => {
    this.book = Object.values(this.coordinators).reduce<Record<string, PublicOrder>>(
      (book, coordinator) => {
        return { ...book, ...coordinator.book };
      },
      {},
    );
    this.exchange.loadingCoordinators =
      this.exchange.loadingCoordinators < 1 ? 0 : this.exchange.loadingCoordinators - 1;
    this.loading = this.exchange.loadingCoordinators > 0;
    this.updateExchange();
    this.triggerHook('onFederationUpdate');
  };

  updateUrl = async (origin: Origin, settings: Settings, hostUrl: string): Promise<void> => {
    const federationUrls = {};
    for (const coor of Object.values(this.coordinators)) {
      coor.updateUrl(origin, settings, hostUrl);
      federationUrls[coor.shortAlias] = coor.url;
    }
    systemClient.setCookie('federation', JSON.stringify(federationUrls));
  };

  update = async (): Promise<void> => {
    this.loading = true;
    this.exchange.info = {
      num_public_buy_orders: 0,
      num_public_sell_orders: 0,
      book_liquidity: 0,
      active_robots_today: 0,
      last_day_nonkyc_btc_premium: 0,
      last_day_volume: 0,
      lifetime_volume: 0,
      version: { major: 0, minor: 0, patch: 0 },
    };
    this.exchange.onlineCoordinators = 0;
    this.exchange.loadingCoordinators = Object.keys(this.coordinators).length;
    this.updateEnabledCoordinators();
    // for (const coor of Object.values(this.coordinators)) {
    //   void coor.update(() => {
    //     this.exchange.onlineCoordinators = this.exchange.onlineCoordinators + 1;
    //     this.onCoordinatorSaved();
    //   });
    // }
  };

  updateBook = async (): Promise<void> => {
    // this.loading = true;
    // this.book = [];
    // this.triggerHook('onFederationUpdate');
    // this.exchange.loadingCoordinators = Object.keys(this.coordinators).length;
    // for (const coor of Object.values(this.coordinators)) {
    //   void coor.updateBook(() => {
    //     this.onCoordinatorSaved();
    //     this.triggerHook('onFederationUpdate');
    //   });
    // }
  };

  updateExchange = (): void => {
    this.exchange.info = updateExchangeInfo(this);
    this.triggerHook('onFederationUpdate');
  };

  // Coordinators
  getCoordinator = (shortAlias: string): Coordinator => {
    return this.coordinators[shortAlias];
  };

  disableCoordinator = (shortAlias: string): void => {
    this.coordinators[shortAlias].disable();
    this.updateEnabledCoordinators();
    this.triggerHook('onFederationUpdate');
  };

  enableCoordinator = (shortAlias: string): void => {
    this.coordinators[shortAlias].enable(() => {
      this.updateEnabledCoordinators();
      this.triggerHook('onFederationUpdate');
    });
  };

  updateEnabledCoordinators = (): void => {
    this.exchange.enabledCoordinators = Object.values(this.coordinators).filter(
      (c) => c.enabled,
    ).length;
    this.triggerHook('onFederationUpdate');
  };
}

export default Federation;
