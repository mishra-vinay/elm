module Main exposing (main)

import Browser
import Browser.Navigation as Nav
import Html exposing (Html, div, text, a, nav, h1, h3, span, ul, li, p)
import Html.Attributes exposing (href, class, style)
import Html.Events exposing (onClick)
import Url exposing (Url)
import Url.Parser as Parser exposing ((</>), s, int, top)
import Maybe
import Pages.Home as Home
import Pages.About as About
import Pages.Contact as Contact
import Pages.ProductDetail as ProductDetail
import Pages.ProductList as ProductList
import Pages.LoanCalculator as LoanCalculator
import Pages.Login as Login
import Pages.Register as Register

-- ROUTES

type Route
    = Home
    | About
    | Contact
    | ProductDetail Int
    | ProductList
    | LoanCalculator
    | Login
    | Register

routeParser : Parser.Parser (Route -> a) a
routeParser =
    Parser.oneOf
        [ Parser.map Home top
        , Parser.map About (s "about")
        , Parser.map Contact (s "contact")
        , Parser.map ProductDetail (s "product" </> int)
        , Parser.map ProductList (s "products")
        , Parser.map LoanCalculator (s "calculator")
        , Parser.map Login (s "login")
        , Parser.map Register (s "register")
        ]

parseUrl : Url -> Route
parseUrl url =
    case Parser.parse routeParser url of
        Just route -> route
        Nothing -> Home

createUrl : String -> String
createUrl path =
    path

-- MODEL

type alias Model =
    { route : Route
    , key : Nav.Key
    , loanCalculatorModel : LoanCalculator.Model
    , contactModel : Contact.Model
    , loginModel : Login.Model
    , registerModel : Register.Model
    }

init : () -> Url -> Nav.Key -> ( Model, Cmd Msg )
init _ url key =
    ( { route = parseUrl url
      , key = key
      , loanCalculatorModel = LoanCalculator.init
      , contactModel = Contact.init
      , loginModel = Login.init
      , registerModel = Register.init
      }
    , Cmd.none
    )

type Msg
    = LinkClicked Browser.UrlRequest
    | UrlChanged Url
    | LoanCalculatorMsg LoanCalculator.Msg
    | ContactMsg Contact.Msg
    | LoginMsg Login.Msg
    | RegisterMsg Register.Msg

update : Msg -> Model -> ( Model, Cmd Msg )
update msg model =
    case msg of
        LinkClicked urlRequest ->
            case urlRequest of
                Browser.Internal url ->
                    ( model, Nav.pushUrl model.key (Url.toString url) )
                Browser.External href ->
                    ( model, Nav.load href )
        UrlChanged url ->
            ( { model | route = parseUrl url }, Cmd.none )
        LoanCalculatorMsg loanMsg ->
            ( { model | loanCalculatorModel = LoanCalculator.update loanMsg model.loanCalculatorModel }, Cmd.none )
        ContactMsg contactMsg ->
            let (newContactModel, cmd) = Contact.update contactMsg model.contactModel in
            ( { model | contactModel = newContactModel }, Cmd.map ContactMsg cmd )
        LoginMsg loginMsg ->
            let (newLoginModel, cmd) = Login.update loginMsg model.loginModel in
            ( { model | loginModel = newLoginModel }, Cmd.map LoginMsg cmd )
        RegisterMsg registerMsg ->
            let (newRegisterModel, cmd) = Register.update registerMsg model.registerModel in
            ( { model | registerModel = newRegisterModel }, Cmd.map RegisterMsg cmd )

subscriptions : Model -> Sub Msg
subscriptions _ = Sub.none

view : Model -> Browser.Document Msg
view model =
    { title = "Elm Website"
    , body =
        [ div [ class "site-header" ]
            [ div [ class "header-top" ]
                [ div [ class "header-top-content" ]
                    [ span [] [ text "Welcome username" ]
                    , div []
                        [ a [ href "/about" ] [ text "About" ]
                        , a [ href "/contact" ] [ text "Contact" ]
                        , a [ href "/login", onClick (LinkClicked (Browser.Internal (Url.fromString (createUrl "/login") |> Maybe.withDefault (Url.fromString (createUrl "/") |> Maybe.withDefault (Debug.todo "Invalid URL"))))) ] [ text "Login" ]
                        , a [ href "/register", onClick (LinkClicked (Browser.Internal (Url.fromString (createUrl "/register") |> Maybe.withDefault (Url.fromString (createUrl "/") |> Maybe.withDefault (Debug.todo "Invalid URL"))))) ] [ text "Register" ]
                        ]
                    ]
                ]
            , div [ class "header-main" ]
                [ div [ class "header-main-content" ]
                    [ a [ href "/", class "logo" ] [ text "Logo" ]
                    , nav [ class "site-nav" ]
                        [ a [ href "/", onClick (LinkClicked (Browser.Internal (Url.fromString (createUrl "/") |> Maybe.withDefault (Url.fromString (createUrl "/") |> Maybe.withDefault (Debug.todo "Invalid URL"))))) ] [ text "Home" ]
                        , a [ href "/about", onClick (LinkClicked (Browser.Internal (Url.fromString (createUrl "/about") |> Maybe.withDefault (Url.fromString (createUrl "/") |> Maybe.withDefault (Debug.todo "Invalid URL"))))) ] [ text "About Us" ]
                        , a [ href "/products", onClick (LinkClicked (Browser.Internal (Url.fromString (createUrl "/products") |> Maybe.withDefault (Url.fromString (createUrl "/") |> Maybe.withDefault (Debug.todo "Invalid URL"))))) ] [ text "Products" ]
                        , a [ href "/calculator", onClick (LinkClicked (Browser.Internal (Url.fromString (createUrl "/calculator") |> Maybe.withDefault (Url.fromString (createUrl "/") |> Maybe.withDefault (Debug.todo "Invalid URL"))))) ] [ text "Loan Calculator" ]
                        , a [ href "/contact", onClick (LinkClicked (Browser.Internal (Url.fromString (createUrl "/contact") |> Maybe.withDefault (Url.fromString (createUrl "/") |> Maybe.withDefault (Debug.todo "Invalid URL"))))) ] [ text "Contact Us" ]
                        ]
                    ]
                ]
            ]
        , div [ class "main-content" ]
            [ case model.route of
                Home -> Home.view
                About -> About.view
                Contact -> Html.map ContactMsg (Contact.view model.contactModel)
                ProductDetail id -> ProductDetail.view id
                ProductList -> ProductList.view
                LoanCalculator -> Html.map LoanCalculatorMsg (LoanCalculator.view model.loanCalculatorModel)
                Login -> Html.map LoginMsg (Login.view model.loginModel)
                Register -> Html.map RegisterMsg (Register.view model.registerModel)
            ]
        , div [ class "site-footer" ]
            [ div [ class "footer-container" ]
                [ div [ class "footer-content" ]
                    [ div [ class "footer-section" ]
                        [ a [ href "/", class "footer-logo" ] [ text "About" ]
                        , p [ style "color" "#bdc3c7", style "font-size" "14px", style "line-height" "1.6" ] 
                            [ text "Your trusted source for modern web applications built with Elm. Discover our products, tools, and services." ]
                        ]
                    , div [ class "footer-section" ]
                        [ h3 [] [ text "Categories" ]
                        , ul []
                            [ li [] [ a [ href "/products" ] [ text "Products" ] ]
                            , li [] [ a [ href "/calculator" ] [ text "Loan Calculator" ] ]
                            , li [] [ a [ href "/about" ] [ text "About Us" ] ]
                            , li [] [ a [ href "/contact" ] [ text "Contact" ] ]
                            ]
                        ]
                    , div [ class "footer-section" ]
                        [ h3 [] [ text "Business" ]
                        , ul []
                            [ li [] [ a [ href "/about" ] [ text "About Us" ] ]
                            , li [] [ a [ href "/contact" ] [ text "Contact Us" ] ]
                            , li [] [ a [ href "/products" ] [ text "Our Products" ] ]
                            , li [] [ a [ href "/calculator" ] [ text "Tools" ] ]
                            ]
                        ]
                    , div [ class "footer-section" ]
                        [ h3 [] [ text "Support" ]
                        , ul []
                            [ li [] [ a [ href "/contact" ] [ text "Get Help" ] ]
                            , li [] [ a [ href "/about" ] [ text "Company Info" ] ]
                            , li [] [ a [ href "/products" ] [ text "Product Support" ] ]
                            , li [] [ a [ href "/calculator" ] [ text "Calculator Help" ] ]
                            ]
                        ]
                    ]
                , div [ class "footer-bottom" ]
                    [ p [] [ text "© 2025 Loan Website. All rights reserved. | Built with TFW" ]
                    ]
                ]
            ]
        ]
    }

main : Program () Model Msg
main =
    Browser.application
        { init = init
        , view = view
        , update = update
        , subscriptions = subscriptions
        , onUrlChange = UrlChanged
        , onUrlRequest = LinkClicked
        } 