module Pages.Contact exposing (view, Model, Msg(..), init, update)

import Html exposing (Html, div, h1, h2, p, input, label, button, textarea, text, form, ul, li, span)
import Html.Attributes exposing (type_, value, placeholder, style, class, for, name, id, required)
import Html.Events exposing (onInput, onClick, onSubmit)
import String

-- MODEL

type alias Model =
    { fullName : String
    , email : String
    , phone : String
    , subject : String
    , message : String
    }

init : Model
init =
    { fullName = ""
    , email = ""
    , phone = ""
    , subject = ""
    , message = ""
    }

-- UPDATE

type Msg
    = UpdateFullName String
    | UpdateEmail String
    | UpdatePhone String
    | UpdateSubject String
    | UpdateMessage String
    | Submit

update : Msg -> Model -> (Model, Cmd Msg)
update msg model =
    case msg of
        UpdateFullName v -> ({ model | fullName = v }, Cmd.none)
        UpdateEmail v -> ({ model | email = v }, Cmd.none)
        UpdatePhone v -> ({ model | phone = v }, Cmd.none)
        UpdateSubject v -> ({ model | subject = v }, Cmd.none)
        UpdateMessage v -> ({ model | message = v }, Cmd.none)
        Submit -> (model, sendMail model)

sendMail : Model -> Cmd Msg
sendMail model =
    let
        mailto =
            "mailto:misvinay@gmail.com"
                ++ "?subject=" ++ String.replace " " "%20" model.subject
                ++ "&body=" ++ String.replace " " "%20" (
                    "Name: " ++ model.fullName ++ "%0A"
                    ++ "Email: " ++ model.email ++ "%0A"
                    ++ (if model.phone /= "" then "Phone: " ++ model.phone ++ "%0A" else "")
                    ++ "Message: " ++ model.message
                )
    in
    Html.Events.onClick (\_ -> ())
    |> always (Cmd.none)
    |> (\_ ->
        let _ = Debug.log "mailto" mailto in
        Cmd.none
    )

-- VIEW

view : Model -> Html Msg
view model =
    div [ class "contact-page" ]
        [ h1 [ style "text-align" "center", style "color" "#1976d2", style "margin-bottom" "32px" ] [ text "Contact Us" ]
        , div [ class "contact-layout" ]
            [ -- Sidebar
              div [ class "contact-sidebar" ]
                [ h2 [ style "color" "#1976d2", style "font-size" "1.3rem", style "margin-bottom" "18px" ] [ text "Australian Loan Companies" ]
                , ul [ class "company-list" ]
                    [ li []
                        [ span [ class "company-name" ] [ text "NAB (National Australia Bank)" ]
                        , p [ class "company-address" ] [ text "700 Bourke St, Docklands VIC 3008, Australia" ]
                        , p [ class "company-phone" ] [ text "Phone: 13 22 65" ]
                        ]
                    , li []
                        [ span [ class "company-name" ] [ text "Commonwealth Bank" ]
                        , p [ class "company-address" ] [ text "201 Sussex St, Sydney NSW 2000, Australia" ]
                        , p [ class "company-phone" ] [ text "Phone: 13 22 21" ]
                        ]
                    , li []
                        [ span [ class "company-name" ] [ text "Westpac" ]
                        , p [ class "company-address" ] [ text "275 Kent St, Sydney NSW 2000, Australia" ]
                        , p [ class "company-phone" ] [ text "Phone: 13 20 32" ]
                        ]
                    , li []
                        [ span [ class "company-name" ] [ text "ANZ" ]
                        , p [ class "company-address" ] [ text "833 Collins St, Docklands VIC 3008, Australia" ]
                        , p [ class "company-phone" ] [ text "Phone: 13 13 14" ]
                        ]
                    ]
                ]
            -- Contact Form
            , form [ onSubmit Submit, class "contact-form" ]
                [ div [ class "form-group" ]
                    [ label [ for "fullName", class "form-label" ] [ text "Full Name*" ]
                    , input [ id "fullName", name "fullName", type_ "text", value model.fullName, onInput UpdateFullName, placeholder "Enter your full name", required True, class "form-input" ] []
                    ]
                , div [ class "form-group" ]
                    [ label [ for "email", class "form-label" ] [ text "Your Email*" ]
                    , input [ id "email", name "email", type_ "email", value model.email, onInput UpdateEmail, placeholder "Enter your email", required True, class "form-input" ] []
                    ]
                , div [ class "form-group" ]
                    [ label [ for "phone", class "form-label" ] [ text "Phone Number (Optional)" ]
                    , input [ id "phone", name "phone", type_ "tel", value model.phone, onInput UpdatePhone, placeholder "Enter your phone number", class "form-input" ] []
                    ]
                , div [ class "form-group" ]
                    [ label [ for "subject", class "form-label" ] [ text "Subject" ]
                    , input [ id "subject", name "subject", type_ "text", value model.subject, onInput UpdateSubject, placeholder "Subject", class "form-input" ] []
                    ]
                , div [ class "form-group" ]
                    [ label [ for "message", class "form-label" ] [ text "How can We Help You?" ]
                    , textarea [ id "message", name "message", value model.message, onInput UpdateMessage, placeholder "Type your message here...", style "min-height" "100px", class "form-input" ] []
                    ]
                , button [ type_ "submit", class "btn-primary", style "margin-top" "18px" ] [ text "Submit Now" ]
                ]
            ]
        ] 